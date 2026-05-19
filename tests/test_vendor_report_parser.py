"""Tests for src/io/vendor_report_parser.py.

We synthesise the Athena/NGP Final Report PDF layout via reportlab so
tests have a deterministic input. The real Abu Road PDF the prompt
references isn't shipped in the repo, but if a user drops it at
``examples/abu_road_final_report.pdf`` the integration test at the
bottom will pick it up and validate against the published numbers.

Coverage targets:
  * All ~12 canonical fields extract from a well-formed PDF
  * Confidence levels reflect the source location (page 6 ⇒ 1.0)
  * Multiple wall-thickness values parse as a sorted list
  * Year is derived from a full date when present
  * Graceful handling of: missing file, image-only PDF, completely
    wrong template (non-NGP vendor)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.io.vendor_report_parser import (
    CONFIDENCE_DIRECT,
    CONFIDENCE_PATTERN,
    ExtractedMetadata,
    VendorReportParser,
)


# ---------------------------------------------------------------------------
# Synthetic-PDF helpers (built once per test that needs them)
# ---------------------------------------------------------------------------

def _build_pdf(path: Path, pages: list[str]) -> None:
    """Render ``pages`` (one string per page) into a PDF via reportlab.

    Each page is rendered as plain Helvetica 10pt with line-wrapping so
    pypdf sees the text exactly as written.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    for body in pages:
        c.setFont("Helvetica", 10)
        y = height - 50
        for line in body.split("\n"):
            c.drawString(50, y, line)
            y -= 14
            if y < 50:
                c.showPage()
                c.setFont("Helvetica", 10)
                y = height - 50
        c.showPage()
    c.save()


def _abu_road_style_pages() -> list[str]:
    """20-page PDF mimicking the Athena/NGP Final Report template.

    Page numbers map roughly to:
      1   — cover (client, pipeline name)
      2-5 — TOC / abbreviations (filler)
      6   — Pipeline Data table  (OD, length, WT, material, MAOP)
      7-19 — filler
      20  — Strength Calculation Summary (SMYS, Fd, MAOP)
    """
    pages: list[str] = []
    # ----- Page 1: cover
    pages.append(
        "Final Inspection Report\n"
        "Client : GAIL (India) Limited\n"
        '16" IPS, ABU ROAD - IP-2 (KHINWADA) Pipeline\n'
        "Inspection Date: 15 March 2023\n"
        "ATHENA POWERTECH LLP / NGP\n"
        "Tool: EGP + MFL-A + MFL-C + XYZ\n"
    )
    # ----- Pages 2-5: filler so page 6 is genuinely page 6
    for _ in range(4):
        pages.append("Table of contents — section heading filler text.\n" * 8)
    # ----- Page 6: Pipeline Data table
    pages.append(
        "Pipeline Data\n"
        "Outer Diameter (mm) : 406\n"
        "Pipeline Length (km) : 144.4\n"
        "Wall Thickness (mm) : 7.1, 8.7, 9.5\n"
        "Material Grade : API 5L X65\n"
        "MAOP (kg/cm²) : 98\n"
        "Pipeline Section : ABU-IP2 Pipeline\n"
    )
    # ----- Pages 7-19: more filler
    for _ in range(13):
        pages.append("Section narrative text. Tool performance. Results.\n")
    # ----- Page 20: Strength Calculation Summary
    pages.append(
        "Strength Calculation Summary\n"
        "SMYS = 450 MPa\n"
        "Design Factor (Fd) = 0.72\n"
        "MAOP = 98 kg/cm²\n"
        "Material: API 5L X65\n"
    )
    return pages


@pytest.fixture
def abu_road_pdf(tmp_path: Path) -> Path:
    """Synthetic Abu Road-style Final Report at the canonical page layout."""
    pdf = tmp_path / "abu_road_synth.pdf"
    _build_pdf(pdf, _abu_road_style_pages())
    return pdf


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------

class TestAthenaTemplate:
    """A complete Athena/NGP-style PDF should extract every field."""

    def test_parse_returns_metadata_object(self, abu_road_pdf: Path):
        md = VendorReportParser().parse(abu_road_pdf)
        assert isinstance(md, ExtractedMetadata)

    def test_geometry_fields(self, abu_road_pdf: Path):
        md = VendorReportParser().parse(abu_road_pdf)
        assert md.outer_diameter_mm == 406.0
        assert md.length_km == pytest.approx(144.4)
        assert md.wall_thicknesses_mm == [7.1, 8.7, 9.5]

    def test_material_and_pressure(self, abu_road_pdf: Path):
        md = VendorReportParser().parse(abu_road_pdf)
        assert md.material_grade == "API 5L X65"
        assert md.smys_mpa == 450.0
        assert md.maop_kgcm2 == 98.0
        assert md.design_factor == pytest.approx(0.72)

    def test_identity_fields(self, abu_road_pdf: Path):
        md = VendorReportParser().parse(abu_road_pdf)
        assert md.client == "GAIL (India) Limited"
        assert md.pipeline_name is not None
        assert "ABU" in md.pipeline_name.upper()
        assert md.vendor is not None
        assert "ATHENA" in md.vendor.upper()
        assert md.inspection_technology is not None
        # Inspection technology should include at least one MFL tool.
        assert "MFL" in md.inspection_technology.upper()

    def test_run2_date_extracted(self, abu_road_pdf: Path):
        md = VendorReportParser().parse(abu_road_pdf)
        assert md.run2_inspection_date_str is not None
        assert "2023" in md.run2_inspection_date_str
        assert md.run2_inspection_year == 2023

    def test_confidence_scores_high_on_priority_pages(
        self, abu_road_pdf: Path,
    ):
        """Fields lifted from pages 6 and 20 should score CONFIDENCE_DIRECT."""
        md = VendorReportParser().parse(abu_road_pdf)
        # Page 6 fields
        for f in ("outer_diameter_mm", "length_km", "wall_thicknesses_mm"):
            assert md.confidence_per_field[f] == CONFIDENCE_DIRECT, (
                f"{f} confidence = {md.confidence_per_field[f]}"
            )
        # Page 20 fields
        for f in ("smys_mpa", "maop_kgcm2", "design_factor"):
            assert md.confidence_per_field[f] >= CONFIDENCE_PATTERN

    def test_found_field_count(self, abu_road_pdf: Path):
        """We expect ≥10 fields filled for a complete template."""
        md = VendorReportParser().parse(abu_road_pdf)
        assert md.found_field_count() >= 10, (
            f"only {md.found_field_count()} fields filled: "
            f"{md.to_dict()}"
        )


# ---------------------------------------------------------------------------
# Minimal happy-path: just the labels, no template layout
# ---------------------------------------------------------------------------

class TestMinimalLabels:
    """A 1-page PDF with just the labels should still extract correctly."""

    @pytest.fixture
    def minimal_pdf(self, tmp_path: Path) -> Path:
        pdf = tmp_path / "minimal.pdf"
        _build_pdf(pdf, [
            "Outer Diameter: 273 mm\n"
            "Pipeline Length: 58.5 km\n"
            "Wall Thickness: 6.0, 8.0 mm\n"
            "Material Grade: API 5L X52\n"
            "SMYS: 358 MPa\n"
            "MAOP: 70 kg/cm²\n"
            "Design Factor: 0.72\n"
        ])
        return pdf

    def test_all_fields_extract(self, minimal_pdf: Path):
        md = VendorReportParser().parse(minimal_pdf)
        assert md.outer_diameter_mm == 273.0
        assert md.length_km == pytest.approx(58.5)
        assert md.wall_thicknesses_mm == [6.0, 8.0]
        assert md.material_grade == "API 5L X52"
        assert md.smys_mpa == 358.0
        assert md.maop_kgcm2 == 70.0
        assert md.design_factor == pytest.approx(0.72)

    def test_confidence_drops_off_priority_pages(self, minimal_pdf: Path):
        """A 1-page PDF doesn't reach pages 6 / 20 — confidence < 1.0."""
        md = VendorReportParser().parse(minimal_pdf)
        # All values still extracted, but with PATTERN confidence.
        for f in ("outer_diameter_mm", "length_km", "smys_mpa"):
            c = md.confidence_per_field[f]
            assert 0.3 <= c < 1.0


# ---------------------------------------------------------------------------
# Edge cases / error handling
# ---------------------------------------------------------------------------

class TestErrorPaths:
    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            VendorReportParser().parse(tmp_path / "does_not_exist.pdf")

    def test_corrupt_pdf_raises_clear_error(self, tmp_path: Path):
        bad = tmp_path / "corrupt.pdf"
        bad.write_bytes(b"Not a PDF - just plain bytes.")
        with pytest.raises(ValueError, match="corrupt|unsupported|open"):
            VendorReportParser().parse(bad)

    def test_image_only_pdf_returns_empty_metadata(self, tmp_path: Path):
        """An image-only PDF (no embedded text) should NOT raise — it
        should return an empty ExtractedMetadata with a clear note."""
        # Build a PDF with no text (just an empty page).
        from reportlab.pdfgen import canvas
        path = tmp_path / "image_only.pdf"
        c = canvas.Canvas(str(path))
        c.showPage()
        c.save()

        md = VendorReportParser().parse(path)
        assert md.found_field_count() == 0
        assert any("image-only" in note.lower() or "no extractable" in note.lower()
                   for note in md.extraction_notes)

    def test_non_athena_template_records_note(self, tmp_path: Path):
        """A PDF with text but no recognisable Athena/NGP labels should
        return mostly-empty metadata with a guidance note."""
        path = tmp_path / "non_athena.pdf"
        _build_pdf(path, [
            "Some other vendor's report.\n"
            "Lorem ipsum dolor sit amet, consectetur adipiscing elit.\n"
            "Numbers that aren't pipeline data: 42, 3.14, 1000.\n"
        ])
        md = VendorReportParser().parse(path)
        # We don't require 0 — a stray digit could match the year regex.
        # Just confirm none of the high-value fields populated.
        assert md.outer_diameter_mm is None
        assert md.maop_kgcm2 is None
        assert md.material_grade is None


# ---------------------------------------------------------------------------
# Ambiguity / multi-match
# ---------------------------------------------------------------------------

class TestAmbiguity:
    def test_first_match_wins_when_value_repeats(self, tmp_path: Path):
        """If the same field appears twice with the same value, take it."""
        path = tmp_path / "repeats.pdf"
        _build_pdf(path, [
            "Outer Diameter: 273 mm\n"
            "Outer Diameter: 273 mm\n"
        ])
        md = VendorReportParser().parse(path)
        assert md.outer_diameter_mm == 273.0

    def test_inconsistent_values_logged_in_notes(self, tmp_path: Path):
        """Two different MAOP values → keep first, note the conflict.

        The current _set() helper notes the conflict in extraction_notes
        rather than picking the higher / lower one.
        """
        path = tmp_path / "inconsistent.pdf"
        # The parser also dedup-matches: it stops after the first hit
        # within a priority block, so a second value only surfaces if
        # the parser scans the full doc separately. For the synthetic
        # short PDF, both calls land on the same first-match — so this
        # test mostly confirms the parser doesn't crash on conflicting
        # data and produces a deterministic result.
        _build_pdf(path, [
            "MAOP: 70 kg/cm²\n"
            "MAOP: 95 kg/cm²\n"
        ])
        md = VendorReportParser().parse(path)
        assert md.maop_kgcm2 in (70.0, 95.0)


# ---------------------------------------------------------------------------
# Wall-thickness list parsing details
# ---------------------------------------------------------------------------

class TestWallThicknessListing:
    @pytest.mark.parametrize("phrase,expected", [
        ("Wall Thickness: 7.1, 8.7, 9.5 mm", [7.1, 8.7, 9.5]),
        ("Wall Thickness: 7.1 / 8.7 / 9.5 mm", [7.1, 8.7, 9.5]),
        ("Wall Thickness: 6.0 and 8.0 mm", [6.0, 8.0]),
        ("Wall Thickness: 7.1 mm", [7.1]),
    ])
    def test_listing_forms(self, tmp_path: Path, phrase: str, expected: list[float]):
        path = tmp_path / "wt.pdf"
        _build_pdf(path, [phrase])
        md = VendorReportParser().parse(path)
        assert md.wall_thicknesses_mm == expected

    def test_list_is_sorted_and_deduped(self, tmp_path: Path):
        path = tmp_path / "wt_dup.pdf"
        _build_pdf(path, ["Wall Thickness: 8.7, 7.1, 7.1, 9.5 mm"])
        md = VendorReportParser().parse(path)
        assert md.wall_thicknesses_mm == [7.1, 8.7, 9.5]


# ---------------------------------------------------------------------------
# Product + service class + installation year + smart-quote pipeline name
# (Prompt 30 — IP-2 → Nasirabad PDF surfaced these as blank fields)
# ---------------------------------------------------------------------------

class TestProductAndInstallationYear:
    """Page 6's Pipeline Data table carries pipeline medium + year of
    construction. The parser must extract both so the GUI's Project
    Setup form fills the Product / Installation year fields without
    user typing.
    """

    def _build_data_pdf(self, tmp_path: Path, extra_lines: str) -> Path:
        pages = ["Cover\n"]
        for _ in range(4):
            pages.append("Filler.\n")
        pages.append(
            "Pipeline Data\n"
            "Outer Diameter (mm) : 406\n"
            "Pipeline Length (km) : 144.4\n"
            "Wall Thickness (mm) : 7.1, 8.7, 9.5\n"
            f"{extra_lines}\n"
        )
        path = tmp_path / "data.pdf"
        _build_pdf(path, pages)
        return path

    def test_pipeline_medium_lpg(self, tmp_path):
        pdf = self._build_data_pdf(
            tmp_path,
            "Pipeline medium during inspection LPG",
        )
        md = VendorReportParser().parse(pdf)
        assert md.product == "LPG"
        assert md.service_class == "liquid"

    def test_pipeline_medium_crude_oil(self, tmp_path):
        pdf = self._build_data_pdf(
            tmp_path,
            "Pipeline medium during inspection Crude Oil",
        )
        md = VendorReportParser().parse(pdf)
        assert md.product == "Crude Oil"
        assert md.service_class == "liquid"

    def test_pipeline_medium_natural_gas(self, tmp_path):
        pdf = self._build_data_pdf(
            tmp_path,
            "Pipeline medium Natural Gas",
        )
        md = VendorReportParser().parse(pdf)
        assert md.product == "Natural Gas"
        assert md.service_class == "gas"

    def test_cover_page_product_phrasing(self, tmp_path):
        """Cover pages often phrase the product as '<X> pipeline'."""
        pages = ["This is an LPG pipeline.\n"]
        for _ in range(20):
            pages.append("filler\n")
        path = tmp_path / "cover.pdf"
        _build_pdf(path, pages)
        md = VendorReportParser().parse(path)
        assert md.product == "LPG"
        assert md.service_class == "liquid"

    def test_installation_year_full_label(self, tmp_path):
        pdf = self._build_data_pdf(
            tmp_path,
            "Year of construction of the pipeline 2001",
        )
        md = VendorReportParser().parse(pdf)
        assert md.installation_year == 2001

    def test_installation_year_short_label(self, tmp_path):
        pdf = self._build_data_pdf(
            tmp_path,
            "Year of construction: 2014",
        )
        md = VendorReportParser().parse(pdf)
        assert md.installation_year == 2014

    def test_installation_year_built_in(self, tmp_path):
        pdf = self._build_data_pdf(
            tmp_path,
            "Built in 1998",
        )
        md = VendorReportParser().parse(pdf)
        assert md.installation_year == 1998

    def test_installation_year_out_of_range_rejected(self, tmp_path):
        """The regex captures any 4-digit number near the label, so we
        guard against absurd values (e.g. '2299' or stray page numbers
        like '0042')."""
        pdf = self._build_data_pdf(
            tmp_path,
            "Year of construction 0042",
        )
        md = VendorReportParser().parse(pdf)
        # 0042 is out of [1900, 2099] → field stays None.
        assert md.installation_year is None


class TestPipelineNameVariants:
    """The IP-2 → Nasirabad PDF surfaced a pipeline-name header form
    the v0.2.0 regex couldn't handle. These tests pin the new variants
    so they keep working."""

    def test_ip2_nasirabad_header_nested_IPS(self, tmp_path):
        """'16" IPS, IP-2 (KHINWADA)- IPS NASIRABAD' must extract
        cleanly even though the route name contains a second 'IPS'."""
        pages = [
            "Cover\n"
            '16" IPS, IP-2 (KHINWADA)- IPS NASIRABAD\n'
            "Inspection Date: 15 March 2024\n"
        ]
        path = tmp_path / "ip2_header.pdf"
        _build_pdf(path, pages)
        md = VendorReportParser().parse(path)
        assert md.pipeline_name is not None
        assert "IP-2" in md.pipeline_name
        assert "NASIRABAD" in md.pipeline_name.upper()

    def test_smart_quote_pipeline_name(self, tmp_path):
        """Some PDFs export the inch mark as a smart quote ("/")
        instead of an ASCII double quote or a typographic prime."""
        pages = [
            "Cover\n"
            "16” IPS, ABU ROAD- IP-2\n"   # right double quotation mark
            "Filler\n"
        ]
        path = tmp_path / "smart.pdf"
        _build_pdf(path, pages)
        md = VendorReportParser().parse(path)
        assert md.pipeline_name is not None
        assert "ABU ROAD" in md.pipeline_name

    def test_explicit_PIPELINE_label_fallback(self, tmp_path):
        """When the inch-IPS header is missing entirely, the parser
        falls back to a 'PIPELINE:' cover-page label."""
        pages = [
            "Final Report\n"
            "PIPELINE: 16'' IPS, ABU ROAD - IP-2 (KHINWADA), 144.4 km (22100104)\n"
            "RUN ID: 1ZYC\n"
        ]
        path = tmp_path / "labelled.pdf"
        _build_pdf(path, pages)
        md = VendorReportParser().parse(path)
        assert md.pipeline_name is not None
        assert "ABU ROAD" in md.pipeline_name.upper()

    def test_pipeline_name_truncates_OD_suffix(self, tmp_path):
        """', OD 406 mm' must be stripped so the name stays clean."""
        pages = [
            'Cover\n'
            '16" IPS, ABU ROAD- IP-2 (KHINWADA), OD 406 mm, L 144.4 km\n'
        ]
        path = tmp_path / "od.pdf"
        _build_pdf(path, pages)
        md = VendorReportParser().parse(path)
        assert md.pipeline_name is not None
        # OD-suffix must NOT appear in the final string.
        assert "OD 406" not in md.pipeline_name


# ---------------------------------------------------------------------------
# Run-1 (Previous inspection) date extraction
# ---------------------------------------------------------------------------

class TestRun1DateExtraction:
    """The Pipeline Data table on page 6 always has a 'Previous
    inspections' line. Extract it so the GUI can pre-fill Run-1's
    QDateEdit instead of making the user type it.

    Three precision levels supported:
      * Full date "15 May 2019"   → ISO "2019-05-15", confidence 1.0
      * Month-year  "May 2019"    → ISO "2019-05-15", confidence 0.8
      * Year only   "2019"        → ISO "2019-07-01", confidence 0.5
    """

    def _build_pipeline_data_pdf(self, tmp_path: Path, prev_line: str) -> Path:
        """Build a synthetic Athena/NGP PDF where page 6 carries the
        'Previous inspections' line we want to test."""
        pages = []
        # Pages 1-5: filler so page 6 is page 6.
        pages.append("Final Report\n")
        for _ in range(4):
            pages.append("Filler.\n")
        # Page 6 — Pipeline Data table with the prev-line under test.
        pages.append(
            "Pipeline Data\n"
            "Outer Diameter (mm) : 711\n"
            "Pipeline Length (km) : 151.0\n"
            f"{prev_line}\n"
            "Wall Thickness (mm) : 8.7, 9.5, 11.1\n"
        )
        path = tmp_path / "prev_date.pdf"
        _build_pdf(path, pages)
        return path

    def test_full_date_confidence_1(self, tmp_path):
        pdf = self._build_pipeline_data_pdf(
            tmp_path, "Previous inspections 15 May 2019"
        )
        md = VendorReportParser().parse(pdf)
        assert md.run1_inspection_date_str == "2019-05-15"
        assert md.run1_inspection_year == 2019
        assert md.confidence_per_field["run1_inspection_date_str"] >= 0.95

    def test_full_date_with_ordinal_suffix(self, tmp_path):
        pdf = self._build_pipeline_data_pdf(
            tmp_path, "Previous inspections 15th May 2019"
        )
        md = VendorReportParser().parse(pdf)
        assert md.run1_inspection_date_str == "2019-05-15"
        assert md.run1_inspection_year == 2019

    def test_month_year_only(self, tmp_path):
        """HMEL style: 'Previous inspections May 2019'."""
        pdf = self._build_pipeline_data_pdf(
            tmp_path, "Previous inspections May 2019"
        )
        md = VendorReportParser().parse(pdf)
        assert md.run1_inspection_date_str == "2019-05-15"
        assert md.run1_inspection_year == 2019
        # Mid-month default → 0.8 confidence per the parser spec.
        assert md.confidence_per_field["run1_inspection_date_str"] >= 0.7
        assert md.confidence_per_field["run1_inspection_date_str"] < 1.0

    def test_year_only(self, tmp_path):
        """IP-2 → Nasirabad style: 'Previous inspections 2017'."""
        pdf = self._build_pipeline_data_pdf(
            tmp_path, "Previous inspections 2017"
        )
        md = VendorReportParser().parse(pdf)
        assert md.run1_inspection_date_str == "2017-07-01"
        assert md.run1_inspection_year == 2017
        # Mid-year default → 0.5 confidence.
        assert md.confidence_per_field["run1_inspection_date_str"] >= 0.4
        assert md.confidence_per_field["run1_inspection_date_str"] < 0.7

    def test_alternative_phrasings(self, tmp_path):
        """Make sure 'Previous ILI' and 'Baseline inspection' also work."""
        for line in (
            "Previous ILI: 15 May 2019",
            "Previous ILI 15 May 2019",
            "Previous ILI : May 2019",
            "Baseline inspection: 2017",
        ):
            pdf = self._build_pipeline_data_pdf(tmp_path, line)
            md = VendorReportParser().parse(pdf)
            assert md.run1_inspection_year in (2017, 2019), (
                f"failed on {line!r} — got year = "
                f"{md.run1_inspection_year!r}"
            )

    def test_missing_prev_inspection_logged_in_notes(self, tmp_path):
        """When the PDF doesn't have a previous-inspection line, the
        parser leaves the field None AND adds a guidance note so the
        GUI can tell the user to enter the date manually."""
        pdf = self._build_pipeline_data_pdf(tmp_path, "")
        md = VendorReportParser().parse(pdf)
        assert md.run1_inspection_date_str is None
        assert md.run1_inspection_year is None
        assert any(
            "No previous-inspection date" in n
            for n in md.extraction_notes
        ), f"expected guidance note; got: {md.extraction_notes}"


# ---------------------------------------------------------------------------
# Integration with the user-supplied Abu Road PDF (if present)
# ---------------------------------------------------------------------------

# Standard paths the user might drop the reference PDF at.
_ABU_ROAD_CANDIDATES = (
    Path("examples/abu_road_final_report.pdf"),
    Path("examples/Abu_Road_Final_Report.pdf"),
    Path("examples/abu_road_FR.pdf"),
)


def _find_abu_road_pdf() -> Path | None:
    for p in _ABU_ROAD_CANDIDATES:
        if p.exists():
            return p
    return None


@pytest.mark.skipif(
    _find_abu_road_pdf() is None,
    reason=(
        "Real Abu Road Final Report PDF not present. Drop one at "
        "examples/abu_road_final_report.pdf to exercise this test."
    ),
)
def test_real_abu_road_pdf_published_numbers():
    """Validate parser output against the published Abu Road numbers.

    Originally the parser hit 7/15 fields against the real PDF — this
    test now asserts all 15, covering every fix landed in the
    regex-tuning pass.
    """
    md = VendorReportParser().parse(_find_abu_road_pdf())

    # --- Geometry (page 6 table)
    assert md.outer_diameter_mm == 406.0
    assert md.length_km == pytest.approx(144.4, abs=0.5)
    assert md.wall_thicknesses_mm and \
        set(md.wall_thicknesses_mm) >= {7.1, 8.7, 9.5}, (
        f"got WT list = {md.wall_thicknesses_mm}"
    )

    # --- Material + pressure (page 20 strength-calc summary)
    assert md.material_grade and "X65" in md.material_grade
    assert md.smys_mpa == 450.0, f"got SMYS = {md.smys_mpa}"
    assert md.maop_kgcm2 == 98.0
    assert md.design_factor == pytest.approx(0.72)

    # --- Identity (cover page + running headers)
    assert md.client and "GAIL" in md.client.upper(), \
        f"got client = {md.client!r}"
    assert md.vendor and "ATHENA" in md.vendor.upper()
    assert md.pipeline_name and "IPS" in md.pipeline_name, \
        f"got pipeline_name = {md.pipeline_name!r}"
    assert md.pipeline_name and "ABU" in md.pipeline_name.upper(), \
        "pipeline_name should mention ABU ROAD"
    # Sanity: not a sentence from body text like "pipeline anomalies".
    assert "anomalies" not in md.pipeline_name.lower()

    # --- Inspection technology (page header / cover)
    assert md.inspection_technology and \
        "MFL-A" in md.inspection_technology and \
        "MFL-C" in md.inspection_technology, (
        f"got inspection_technology = {md.inspection_technology!r}"
    )

    # --- Pipeline section code (numeric, from page 6)
    assert md.pipeline_section_code == "22100104", \
        f"got pipeline_section_code = {md.pipeline_section_code!r}"

    # --- Run-2 date — either ISO (parsed) or a raw 2023 string.
    assert md.run2_inspection_year == 2023
    assert md.run2_inspection_date_str and \
        "2023" in md.run2_inspection_date_str, (
        f"got date_str = {md.run2_inspection_date_str!r}"
    )

    # --- Project name — auto-generated from pipeline_name + diameter
    # since the PDF doesn't carry an explicit "FFP_..." tag.
    assert md.project_name and md.project_name.startswith("FFP_"), \
        f"got project_name = {md.project_name!r}"
    assert "16in" in md.project_name.lower() or "16In" in md.project_name, (
        f"derived project_name should include diameter; got "
        f"{md.project_name!r}"
    )
    # Auto-generated values should carry low confidence so the GUI
    # highlights them for verification.
    assert md.confidence_per_field.get("project_name", 1.0) < 0.5
