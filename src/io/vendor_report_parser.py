"""Extract pipeline metadata from an Athena/NGP Final Report PDF.

Athena PowerTech / NGP-format Final Reports follow a fixed template
where the same labels appear in the same positions across every project:

    Cover page (page 1)  — pipeline name, client, vendor logo, year
    Pipeline Data table (~page 6) — OD, length, WT list, material, MAOP
    Strength calc summary (~page 20) — SMYS, design factor, MAOP, Fd

This module turns those PDFs into ~12 pre-filled GUI fields so users
don't have to re-type values that the vendor already typed.

Strategy
--------
1. Read every page of the PDF as plain text via :mod:`pypdf`.
2. For each canonical field, run a list of regex patterns ordered from
   most-specific (exact label, exact format) to least-specific (any
   pattern that mentions the field name nearby).
3. Each match is scored on a 0.0-1.0 confidence scale; ties go to the
   first pattern that matched.

The output is JSON-friendly so the GUI can serialise it for the
preview-dialog and the user can edit anything before applying. If a
field can't be extracted it's omitted from the result rather than set
to a guess.

Out of scope
------------
* Image-based / scanned PDFs (no OCR).
* Vendor templates other than Athena/NGP — Rosen, Baker Hughes, etc.
  use different layouts that need their own regex packs.
* Per-zone different MAOPs — the GUI handles that with one click on
  "Add zone" if the user needs more rows than the parser inferred.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ExtractedMetadata:
    """Pipeline metadata pulled out of one vendor Final Report PDF."""

    # Project / pipeline identity ------------------------------------------
    project_name: str | None = None
    pipeline_name: str | None = None
    pipeline_section_code: str | None = None      # e.g. "ABU-IP2"
    client: str | None = None
    vendor: str | None = None                     # e.g. "ATHENA POWERTECH LLP / NGP"
    inspection_technology: str | None = None      # e.g. "MFL-A + MFL-C"

    # Geometry --------------------------------------------------------------
    outer_diameter_mm: float | None = None
    length_km: float | None = None
    wall_thicknesses_mm: list[float] | None = None  # multiple WTs ⇒ multiple zones
    installation_year: int | None = None          # "Year of construction" / "built"

    # Material + pressure --------------------------------------------------
    material_grade: str | None = None             # "API 5L X65"
    smys_mpa: float | None = None
    maop_kgcm2: float | None = None
    design_factor: float | None = None

    # Product / service class ----------------------------------------------
    # Free-text product description (e.g. "Crude Oil", "LPG") and the
    # broader service class derived from it ("liquid" / "gas" /
    # "multiphase"). The reader / project YAML uses both.
    product: str | None = None
    service_class: str | None = None

    # Run-2 inspection (this PDF describes Run-2) ------------------------
    run2_inspection_year: int | None = None
    run2_inspection_date_str: str | None = None

    # Run-1 inspection — pulled from the "Previous inspections" line in
    # the Pipeline Data table (page ~6 of Athena/NGP Final Reports).
    # Confidence semantics:
    #   1.0   full "15 May 2019" date          → ISO "2019-05-15"
    #   0.8   "May 2019" month-year only       → ISO "2019-05-15" (mid-month)
    #   0.5   "2019" year only                 → ISO "2019-07-01" (mid-year)
    run1_inspection_year: int | None = None
    run1_inspection_date_str: str | None = None

    # Provenance -----------------------------------------------------------
    # Per-field confidence 0.0..1.0 (1.0 = direct label-value match in
    # the expected position, 0.6 = pattern found elsewhere, 0.4 = several
    # candidates found, picked one with caveat flagged in notes).
    confidence_per_field: dict[str, float] = field(default_factory=dict)
    # Human-readable breadcrumbs: "found OD=406 on page 6", etc.
    extraction_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly dict — drops the dataclass machinery."""
        d = asdict(self)
        return d

    def found_field_count(self) -> int:
        """How many canonical fields produced a value (helps the UI label)."""
        skip = {"confidence_per_field", "extraction_notes"}
        return sum(
            1 for k, v in self.__dict__.items()
            if k not in skip and v not in (None, [], {})
        )


# ---------------------------------------------------------------------------
# Confidence levels
# ---------------------------------------------------------------------------

CONFIDENCE_DIRECT = 1.0       # exact label-value match in expected position
CONFIDENCE_PATTERN = 0.6      # pattern match anywhere in the PDF
CONFIDENCE_AMBIGUOUS = 0.4    # multiple candidates found; we picked one
CONFIDENCE_INFERRED = 0.5     # value derived (e.g. year from a full date)


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
#
# All patterns are run on the concatenated text of the whole PDF.
# We pre-normalise the text by collapsing repeated whitespace, but keep
# newlines so multi-line headers like "MAOP\n(kg/cm²)" still resolve.

# Numbers with optional decimal + thousand separators ("1,234.5", "98",
# "0.72"). The regex deliberately doesn't anchor — substring matches
# fine inside larger strings.
_NUM = r"(\d{1,5}(?:[.,]\d+)?)"
_NUM_LIST = r"((?:\d{1,3}(?:[.,]\d+)?\s*(?:,|/|&|and)\s*){1,5}\d{1,3}(?:[.,]\d+)?)"
_INT = r"(\d{4})"


def _to_float(raw: str) -> float | None:
    """Convert a regex group to float, handling commas and stray spaces."""
    if raw is None:
        return None
    s = raw.strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _normalise(text: str) -> str:
    """Collapse runs of spaces, normalise newlines, strip carriage returns.

    Preserves newline structure so multi-line labels still match.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Replace runs of 2+ spaces (but not newlines) with a single space.
    text = re.sub(r"[ \t]+", " ", text)
    return text


def _slugify_pipeline_for_project(
    pipeline_name: str, diameter_mm: float | None,
) -> str | None:
    """Derive a project_name slug from the pipeline name + diameter.

    Used as a low-confidence fallback when the PDF doesn't carry an
    explicit "FFP_..." tag (the typical case for Athena Final Reports —
    that tag is internal to Athena).

    Examples:
        '16″ IPS, ABU ROAD- IP-2 (KHINWADA)' + 406 → 'FFP_AbuRoad_IP-2_16in'
    """
    if not pipeline_name:
        return None
    name = pipeline_name
    # Drop the leading "<inches>″ IPS," prefix if present.
    name = re.sub(r"^\s*\d+\s*[\"′″']\s*IPS\s*,?\s*", "", name)
    # Drop any trailing geometry chatter that slipped through.
    for cutoff in (", OD ", ", L ", " Pipeline", " section"):
        if cutoff in name:
            name = name.split(cutoff, 1)[0]
    # Drop parenthetical asides ("(KHINWADA)") — keeps the slug short.
    name = re.sub(r"\s*\([^)]*\)\s*", " ", name)
    name = name.strip().strip(",-").strip()
    if not name:
        return None

    # Smart capitalisation: title-case only "real" words. Short
    # alphanumeric tokens (e.g. "IP-2", "X65") stay uppercase because
    # they're identifiers, not English words.
    def _cap(word: str) -> str:
        alpha = "".join(c for c in word if c.isalpha())
        if not alpha:
            return word
        # All-upper alpha portion + length > 2 → English word, title-case it.
        if alpha.isupper() and len(alpha) > 2:
            return word.capitalize()
        return word

    title_parts = [_cap(w) for w in name.split()]
    # Strip stray trailing hyphens from individual tokens (e.g. "ROAD-").
    title_parts = [p.rstrip("-_") for p in title_parts]
    slug = "_".join(p for p in title_parts if p)
    slug = re.sub(r"[^A-Za-z0-9_\-]", "", slug)
    if not slug:
        return None
    parts = ["FFP", slug]
    if diameter_mm:
        try:
            inches = int(round(float(diameter_mm) / 25.4))
            if inches > 0:
                parts.append(f"{inches}in")
        except (TypeError, ValueError):
            pass
    return "_".join(parts)


# Ordinal-suffix stripper (1st, 2nd, 3rd, 4th, 18th, …).
_ORDINAL_RE = re.compile(r"(\d+)(?:st|nd|rd|th)", re.IGNORECASE)
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _parse_date_to_iso(raw: str) -> str | None:
    """Best-effort parse of a date string to ``YYYY-MM-DD``.

    Returns ``None`` if no recognised format matches; the caller should
    keep the raw string in that case.
    """
    import datetime as _dt
    if not raw:
        return None
    # Strip ordinal suffixes ("18th August, 2023" → "18 August, 2023").
    cleaned = _ORDINAL_RE.sub(r"\1", raw).strip()
    # Remove the comma between day-month and year.
    cleaned = cleaned.replace(",", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for fmt in (
        "%d %B %Y", "%d %b %Y",                # "18 August 2023"
        "%B %d %Y", "%b %d %Y",                # "August 18 2023"
        "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y",
        "%d-%b-%Y", "%d-%B-%Y",
        "%d.%m.%Y", "%Y.%m.%d",
    ):
        try:
            d = _dt.datetime.strptime(cleaned, fmt).date()
            return d.isoformat()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class VendorReportParser:
    """Extract :class:`ExtractedMetadata` from a vendor Final Report PDF.

    Parameters
    ----------
    pipeline_data_page : int, optional
        1-indexed page that contains the "Pipeline Data" table. Defaults
        to 6 (the Athena/NGP template's standard position). The parser
        still scans the whole document; this is just where it weights
        matches highest.
    strength_page : int, optional
        1-indexed page with the strength-calc summary. Defaults to 20.
    """

    def __init__(
        self,
        *,
        pipeline_data_page: int = 6,
        strength_page: int = 20,
    ) -> None:
        self._pipeline_data_page = pipeline_data_page
        self._strength_page = strength_page

    # ------------------------------------------------------------ public API

    def parse(self, pdf_path: str | Path) -> ExtractedMetadata:
        """Parse the PDF and return the extracted metadata."""
        path = Path(pdf_path)
        if not path.exists():
            raise FileNotFoundError(f"PDF not found: {path}")

        # Lazy import so the rest of the codebase doesn't pull in pypdf
        # at module-load time.
        try:
            from pypdf import PdfReader
        except ImportError as e:                                  # pragma: no cover
            raise ImportError(
                "pypdf is required for PDF parsing. Install with: "
                "pip install pypdf"
            ) from e

        try:
            reader = PdfReader(str(path))
        except Exception as e:                                    # noqa: BLE001
            raise ValueError(
                f"Could not open PDF (corrupt or unsupported): {e}"
            ) from e

        # Pull text per page so we know WHERE a match was found.
        pages: list[str] = []
        for p in reader.pages:
            try:
                pages.append(_normalise(p.extract_text() or ""))
            except Exception:                                     # noqa: BLE001
                pages.append("")

        all_text = "\n\n".join(pages)
        if not all_text.strip():
            # Image-only PDF — nothing to extract.
            md = ExtractedMetadata()
            md.extraction_notes.append(
                "PDF contained no extractable text (image-only / scanned). "
                "Auto-fill not possible — fill the form manually."
            )
            return md

        md = ExtractedMetadata()

        # Order matters: extract identity fields first (used in notes),
        # then geometry/material/pressure, then dates. Project-name
        # auto-generation runs LAST so it can see the diameter we
        # extracted in the geometry pass.
        self._extract_identity(md, pages, all_text)
        self._extract_geometry(md, pages, all_text)
        self._extract_material_pressure(md, pages, all_text)
        self._extract_run2_date(md, pages, all_text)
        self._extract_run1_date(md, pages, all_text)
        self._derive_project_name_if_missing(md)

        # Sanity check — if literally nothing matched, the PDF is
        # probably from a non-Athena vendor.
        if md.found_field_count() == 0:
            md.extraction_notes.append(
                "No recognised Athena/NGP labels found. This PDF may use "
                "a different vendor template. Fill the form manually."
            )

        return md

    # -------------------------------------------------------------- helpers

    def _set(
        self,
        md: ExtractedMetadata,
        field_name: str,
        value: Any,
        confidence: float,
        note: str = "",
    ) -> None:
        """Set ``md.<field_name>`` once, recording confidence + note."""
        existing = getattr(md, field_name, None)
        if existing not in (None, [], {}):
            # Already set by a more-specific rule earlier — don't
            # downgrade, but flag if values disagree.
            if existing != value:
                md.extraction_notes.append(
                    f"{field_name}: kept {existing!r}, also saw {value!r}"
                )
            return
        setattr(md, field_name, value)
        md.confidence_per_field[field_name] = confidence
        if note:
            md.extraction_notes.append(note)

    # ------------------------------------------------------------- identity

    def _extract_identity(
        self, md: ExtractedMetadata, pages: list[str], text: str,
    ) -> None:
        """Project / pipeline / client / vendor / inspection technology."""

        # ----- Client.
        # Two shapes seen in real NGP / Athena Final Reports:
        #   "Client : GAIL (India) Limited"     — synthetic / cover form
        #   "CUSTOMER: GAIL (INDIA) LTD."       — Abu Road real PDF
        # The all-caps form requires re.IGNORECASE on the label, and
        # the trailing newline must be optional because some PDF
        # extractors strip line breaks.
        m = re.search(
            r"(?:Client|Customer|Operator)\s*[:\-]?\s*"
            r"([A-Za-z][^\n]{2,80}?)\s*(?:\n|$)",
            text,
            re.IGNORECASE,
        )
        if m:
            self._set(md, "client", m.group(1).strip(),
                      CONFIDENCE_DIRECT, "Client read from cover page.")

        # ----- Pipeline name.
        # NGP page headers carry the canonical name on every page from
        # page 2 onwards, e.g.
        #     16″ IPS, ABU ROAD- IP-2 (KHINWADA), OD 406 mm, L 144.4 km
        # The format is always <digits><inch-quote><space-or-comma>IPS,
        # then the route name in CAPS, then optionally an OD / L suffix
        # we strip off so the name stays clean.
        #
        # Requiring "IPS" rules out false positives like "270 pipeline
        # anomalies were detected" — the old, looser regex that allowed
        # "pipeline" anywhere is what broke on the real PDF.
        m = re.search(
            r"(\d{1,3}\s*[\"“”′″'`]\s*IPS[,\s]+[A-Z][^\n]{3,160})",
            text,
        )
        if m:
            name = re.sub(r"\s+", " ", m.group(1)).strip()
            # Trim trailing ", OD 406 mm, L 144.4 km" suffix when present —
            # those are geometry, not part of the pipeline identifier.
            for cutoff in (", OD ", ", L ", ", OD ", ", L "):
                if cutoff in name:
                    name = name.split(cutoff, 1)[0]
            self._set(md, "pipeline_name", name,
                      CONFIDENCE_DIRECT, "Pipeline name from page header.")
        else:
            # Looser fallback 1: inch-quote IPS without the immediate
            # capital-letter requirement. Handles smart-quote PDFs.
            m = re.search(
                r"(\d{1,3}\s*[\"“”′″'`]\s*IPS[^\n]{3,160})",
                text,
            )
            if m:
                name = re.sub(r"\s+", " ", m.group(1)).strip()
                for cutoff in (", OD ", ", L "):
                    if cutoff in name:
                        name = name.split(cutoff, 1)[0]
                self._set(md, "pipeline_name", name,
                          CONFIDENCE_PATTERN, "Pipeline name from cover.")
            else:
                # Fallback 2: explicit "PIPELINE:" cover-page label.
                # NGP covers like "PIPELINE: 16'' IPS, ABU ROAD - IP-2 ..."
                # produce this pattern when the page-header form fails.
                m = re.search(
                    r"PIPELINE\s*[:\-]\s*([^\n]{5,160})",
                    text,
                )
                if not m:
                    # Fallback 3: "Pipeline name:" / "Pipeline section:"
                    m = re.search(
                        r"Pipeline\s+(?:name|section)\s*[:\-]\s*([^\n]{5,160})",
                        text,
                        re.IGNORECASE,
                    )
                if m:
                    name = re.sub(r"\s+", " ", m.group(1)).strip()
                    for cutoff in (", OD ", ", L "):
                        if cutoff in name:
                            name = name.split(cutoff, 1)[0]
                    self._set(md, "pipeline_name", name,
                              CONFIDENCE_PATTERN,
                              "Pipeline name from labelled line.")

        # ----- Pipeline section code.
        # Two shapes seen so far:
        #   Numeric:  "Pipeline Section code 22100104"      — Abu Road
        #   Letter:   "ABU-IP2 Pipeline" / "..-XX Section"  — synthetic
        # Try numeric first (more specific), then the letter form as a
        # fallback for synthetic / older templates.
        m = re.search(
            r"section\s+code\s*[:\-]?\s*(\d{6,10})",
            text,
            re.IGNORECASE,
        )
        if m:
            self._set(md, "pipeline_section_code", m.group(1),
                      CONFIDENCE_DIRECT, "Pipeline section code.")
        else:
            m = re.search(
                r"\b([A-Z]{2,6}[\-\s][A-Z0-9]{1,8})\s+(?:Pipeline|Section|Spool)",
                text,
            )
            if m:
                self._set(md, "pipeline_section_code", m.group(1).strip(),
                          CONFIDENCE_PATTERN)

        # ----- Vendor (the report writer).
        for vendor_pattern, confidence in (
            (r"(ATHENA\s+POWER\s*TECH[^\n]{0,40}NGP)", CONFIDENCE_DIRECT),
            (r"(ATHENA\s+POWER\s*TECH(?:\s+LLP)?)", CONFIDENCE_DIRECT),
            (r"(NGP\s+(?:Geo-?Spatial|Pipeline)[^\n]*)", CONFIDENCE_PATTERN),
        ):
            m = re.search(vendor_pattern, text, re.IGNORECASE)
            if m:
                self._set(md, "vendor",
                          re.sub(r"\s+", " ", m.group(1)).strip(),
                          confidence)
                break

        # ----- Inspection technology (MFL-A, MFL-C, EGP, XYZ, IMU…).
        # The real Abu Road PDF prints this string at the top of every
        # page WITHOUT a "Tool:" label:
        #     EGP+MFL-A+ MFL-C+XYZ
        #     EGP+MFL-A+MFL-C +XYZ
        # The synthetic PDF uses "Tool: EGP + MFL-A + MFL-C + XYZ".
        # The new pattern finds two-or-more MFL-X segments joined by
        # '+' anywhere in the doc, optionally preceded by 'EGP+' and
        # followed by '+XYZ'. Whitespace around '+' is permissive.
        m = re.search(
            r"((?:EGP\s*\+\s*)?MFL-[A-Z](?:\s*\+\s*MFL-[A-Z])+"
            r"(?:\s*\+\s*XYZ)?)",
            text,
        )
        if m:
            raw = m.group(1).strip()
            # Normalise spacing around '+' for display.
            clean = re.sub(r"\s*\+\s*", "+", raw)
            self._set(md, "inspection_technology", clean,
                      CONFIDENCE_DIRECT, f"Inspection tech = {clean}")
        else:
            # Fallback: label-bound match for vendors that include the
            # token in plain English.
            m = re.search(
                r"(?:Tool|Technology|Inspection)\s*[:\-]?\s*"
                r"((?:[A-Z]{2,6}[\-/]?[A-Z]?\s*\+?\s*){1,6})",
                text,
            )
            if m:
                raw = m.group(1).strip()
                if "+" in raw or any(
                    tok in raw.upper() for tok in
                    ("MFL", "EGP", "UT", "IMU", "XYZ")
                ):
                    clean = re.sub(r"\s*\+\s*", "+", raw)
                    clean = re.sub(r"\s+", " ", clean).strip(" +-")
                    self._set(md, "inspection_technology", clean,
                              CONFIDENCE_PATTERN)

        # ----- Project name (Athena's "FFP_<name>" tag).
        # Only the EXPLICIT tag is captured here. The slug-from-pipeline
        # fallback runs at the end of parse() so it can see the
        # geometry too (diameter_mm wouldn't be set yet at this point).
        m = re.search(
            r"\b(FFP[_\-][A-Za-z0-9_\-]{4,60})\b",
            text,
        )
        if m:
            self._set(md, "project_name", m.group(1),
                      CONFIDENCE_PATTERN)

    def _derive_project_name_if_missing(self, md: ExtractedMetadata) -> None:
        """Final-pass fallback: synthesise a project name from the
        pipeline name + diameter when the PDF didn't carry an explicit
        "FFP_..." tag. Confidence is 0.3 so the GUI flags it for
        verification.
        """
        if md.project_name:
            return
        if not md.pipeline_name:
            return
        derived = _slugify_pipeline_for_project(
            md.pipeline_name, md.outer_diameter_mm,
        )
        if derived:
            self._set(md, "project_name", derived, 0.3,
                      "Auto-generated project name from pipeline_name "
                      "(low confidence — please verify).")

    # -------------------------------------------------------------- geometry

    def _extract_geometry(
        self, md: ExtractedMetadata, pages: list[str], text: str,
    ) -> None:
        """Outer diameter, pipeline length, list of wall thicknesses."""

        # Prefer the Pipeline Data page if it exists.
        priority_text = self._page_text(pages, self._pipeline_data_page)

        # ----- Outer diameter.
        for src_text, conf in self._priority_corpora(priority_text, text):
            m = re.search(
                r"(?:Outer\s+Diameter|Outside\s+Diameter|Nominal\s+Diameter|"
                r"Pipe\s+OD|\bOD\b)\s*(?:\([^)]*\))?\s*[:\-]?\s*"
                + _NUM + r"\s*(?:mm|MM)?",
                src_text,
                re.IGNORECASE,
            )
            if m:
                v = _to_float(m.group(1))
                if v and 50 <= v <= 2000:
                    self._set(md, "outer_diameter_mm", v, conf,
                              f"OD = {v} mm")
                    break

        # ----- Length (km). Two label-vs-unit shapes seen in real
        #       NGP reports:
        #           (a) "Pipeline Length: 144.4 km"        — unit trails
        #           (b) "Pipeline Length (km) : 144.4"     — unit in parens
        length_labels = (
            r"(?:Total\s+Length|Pipeline\s+Length|"
            r"Length\s+of\s+the\s+pipeline|Approx\.\s+length|Length)"
        )
        for src_text, conf in self._priority_corpora(priority_text, text):
            # Form (b) — unit in parens, value follows colon.
            m = re.search(
                length_labels + r"\s*\(\s*km\s*\)\s*[:\-]?\s*" + _NUM,
                src_text,
                re.IGNORECASE,
            )
            if not m:
                # Form (a) — unit trails the number.
                m = re.search(
                    length_labels + r"\s*[:\-]?\s*" + _NUM + r"\s*(?:km|KM|Km)\b",
                    src_text,
                    re.IGNORECASE,
                )
            if m:
                v = _to_float(m.group(1))
                if v and 0.1 <= v <= 5000:
                    self._set(md, "length_km", v, conf, f"Length = {v} km")
                    break

        # ----- Wall thicknesses (single value OR comma-separated list).
        wts = self._extract_wall_thicknesses(priority_text, text)
        if wts:
            conf = CONFIDENCE_DIRECT if priority_text else CONFIDENCE_PATTERN
            self._set(md, "wall_thicknesses_mm", wts, conf,
                      f"Wall thicknesses = {wts}")

        # ----- Year of construction / installation year.
        # NGP Final Reports state this on page 6: "Year of construction
        # of the pipeline 2001". The label varies a bit between vendors
        # so we accept a few aliases. Range-checked to 1900-2099 so a
        # stray "page 2020" footer can't pollute the field.
        for src_text, conf in self._priority_corpora(priority_text, text):
            m = re.search(
                r"(?:Year\s+of\s+construction(?:\s+of\s+the\s+pipeline)?|"
                r"Construction\s+year|Year\s+(?:built|of\s+installation)|"
                r"Installation\s+year|Built\s+in)\s*[:\-]?\s*(\d{4})",
                src_text,
                re.IGNORECASE,
            )
            if m:
                yr = int(m.group(1))
                if 1900 <= yr <= 2099:
                    self._set(md, "installation_year", yr, conf,
                              f"Installation year = {yr}")
                    break

        # ----- Product / service class.
        self._extract_product(md, priority_text, text)

    # ------------------------------------------------------------- product

    # Map free-text product strings to canonical service classes used
    # in the project YAML / pipeline model. Keys are LOWERCASE; the
    # value is (service_class, canonical_product_display).
    _PRODUCT_CLASS_MAP: dict[str, tuple[str, str]] = {
        "lpg":              ("liquid", "LPG"),
        "crude oil":        ("liquid", "Crude Oil"),
        "crude":            ("liquid", "Crude Oil"),
        "diesel":           ("liquid", "Diesel"),
        "petrol":           ("liquid", "Petrol"),
        "gasoline":         ("liquid", "Gasoline"),
        "kerosene":         ("liquid", "Kerosene"),
        "jet fuel":         ("liquid", "Jet Fuel"),
        "aviation fuel":    ("liquid", "Aviation Fuel"),
        "refined product":  ("liquid", "Refined Product"),
        "naphtha":          ("liquid", "Naphtha"),
        "fuel oil":         ("liquid", "Fuel Oil"),
        "natural gas":      ("gas",    "Natural Gas"),
        "gas":              ("gas",    "Gas"),
        "ng":               ("gas",    "Natural Gas"),
        "multiphase":       ("multiphase", "Multiphase"),
        "water":            ("liquid", "Water"),
    }

    def _extract_product(
        self,
        md: ExtractedMetadata,
        priority_text: str,
        text: str,
    ) -> None:
        """Pull the pipeline's product from the page-6 Pipeline-Data table.

        Three shapes seen in real NGP reports:
          1. "Pipeline medium during inspection  LPG"       — table row
          2. "Pipeline medium  Crude Oil"                   — shorter label
          3. "LPG pipeline" / "crude oil pipeline"          — cover-page header

        Each match also sets `service_class` based on
        :data:`_PRODUCT_CLASS_MAP`, so the GUI can populate both fields
        without a second guess. Unrecognised tokens (e.g. "ammonia") are
        still captured into `product` but `service_class` is left None
        for the user to set manually.
        """
        token_alt = "|".join(
            re.escape(k) for k in sorted(
                self._PRODUCT_CLASS_MAP, key=len, reverse=True,
            )
        )

        # Shape 1 / 2: "Pipeline medium ... <token>"
        for src_text, conf in self._priority_corpora(priority_text, text):
            m = re.search(
                r"Pipeline\s+medium(?:\s+during\s+inspection)?\s*[:\-]?\s*"
                r"(" + token_alt + r")",
                src_text,
                re.IGNORECASE,
            )
            if m:
                self._record_product(md, m.group(1), conf)
                return

        # Shape 3: "<token> pipeline" / "Pipeline for <token>"
        for src_text, conf in self._priority_corpora(priority_text, text):
            m = re.search(
                r"\b(" + token_alt + r")\s+(?:pipeline|line)\b",
                src_text,
                re.IGNORECASE,
            )
            if m:
                self._record_product(md, m.group(1), conf)
                return

        # Fallback: "Service ... liquid|gas|multiphase"
        m = re.search(
            r"Service(?:\s+class)?\s*[:\-]?\s*(liquid|gas|multiphase)",
            text,
            re.IGNORECASE,
        )
        if m:
            sc = m.group(1).lower()
            self._set(md, "service_class", sc, CONFIDENCE_PATTERN,
                      f"Service class = {sc} (from explicit label).")

    def _record_product(
        self, md: ExtractedMetadata, raw_token: str, confidence: float,
    ) -> None:
        """Store the product display string + map it to a service class."""
        key = raw_token.strip().lower()
        sc, display = self._PRODUCT_CLASS_MAP.get(key, (None, raw_token.strip()))
        self._set(md, "product", display, confidence,
                  f"Product = {display}")
        if sc:
            self._set(md, "service_class", sc, confidence,
                      f"Service class = {sc} (derived from {display!r}).")

    def _extract_wall_thicknesses(
        self, priority_text: str, full_text: str,
    ) -> list[float] | None:
        """Three-pass match — list / line-scan / single-value.

        Real NGP PDFs format WT as "7.1 mm/8.7 mm/9.5 mm" (with the
        unit AFTER each value, not just at the end). The list-with-
        separators pattern handles "7.1, 8.7, 9.5"; the line-scan pass
        added below handles the slash-with-trailing-unit form by
        grabbing all decimals from the matched line.
        """
        for src_text in (priority_text, full_text):
            if not src_text:
                continue

            # Pass 1 — explicit list with separators:
            # "Wall thickness: 7.1, 8.7, 9.5 mm" / "... / ..." / "... and ..."
            m = re.search(
                r"Wall\s+Thickness(?:es)?\s*(?:\([^)]*\))?\s*[:\-]?\s*"
                + _NUM_LIST + r"\s*(?:mm|MM)?",
                src_text,
                re.IGNORECASE,
            )
            if m:
                values = self._parse_number_list(m.group(1))
                if values:
                    return values

            # Pass 2 — line scan: find the "wall thickness" line and
            # collect every decimal on it. Catches the real NGP form:
            #     Pipe wall thickness 7.1 mm/8.7 mm/9.5 mm
            # where each value carries its own unit so the list-form
            # separator regex fails.
            m = re.search(
                r"(?:Pipe\s+)?Wall\s+Thickness(?:es)?[^\n]{0,200}",
                src_text,
                re.IGNORECASE,
            )
            if m:
                line = m.group(0)
                decimals = re.findall(r"\d{1,2}\.\d+", line)
                values = sorted({
                    v for v in (_to_float(d) for d in decimals)
                    if v and 1.0 <= v <= 50.0
                })
                if values:
                    return values

            # Pass 3 — single-value form: "Wall thickness 7.1 mm"
            m = re.search(
                r"Wall\s+Thickness\s*(?:\([^)]*\))?\s*[:\-]?\s*"
                + _NUM + r"\s*(?:mm|MM)\b",
                src_text,
                re.IGNORECASE,
            )
            if m:
                v = _to_float(m.group(1))
                if v and 1.0 <= v <= 50.0:
                    return [v]

        return None

    @staticmethod
    def _parse_number_list(raw: str) -> list[float]:
        """Split '7.1, 8.7, 9.5' / '7.1 / 8.7 / 9.5' into floats."""
        tokens = re.split(r"\s*(?:,|/|&|and)\s*", raw.strip())
        out: list[float] = []
        for t in tokens:
            v = _to_float(t)
            if v is not None and 1.0 <= v <= 50.0:
                out.append(v)
        return sorted(set(out))

    # ------------------------------------------------------ material + pressure

    def _extract_material_pressure(
        self, md: ExtractedMetadata, pages: list[str], text: str,
    ) -> None:
        """Material grade, SMYS, MAOP, design factor."""

        priority_text = self._page_text(pages, self._strength_page)

        # ----- Material grade (API 5L X<number>, B, or A25).
        for src_text, conf in self._priority_corpora(priority_text, text):
            m = re.search(
                r"(API\s+5L\s+(?:X\s?\d{2,3}|B|A25|A))",
                src_text,
                re.IGNORECASE,
            )
            if m:
                grade = re.sub(r"\s+", " ", m.group(1).upper()).strip()
                # Tighten "X 65" -> "X65"
                grade = re.sub(r"X\s+(\d)", r"X\1", grade)
                self._set(md, "material_grade", grade, conf,
                          f"Material grade = {grade}")
                break

        # ----- SMYS (MPa). Several real-world phrasings:
        #   (a) "SMYS = 450 MPa"
        #   (b) "Specified Minimum Yield Strength: 450 MPa"
        #   (c) "...having a Specified Minimum Yield Strength (SMYS)
        #        of 450 Mpa."        ← Abu Road real PDF, lowercase Mpa
        # The new regex tolerates up to ~40 non-digit characters between
        # the label and the number — that covers parenthetical "(SMYS)"
        # + " of " glue in form (c) — and uses re.IGNORECASE so "Mpa",
        # "mpa", "MPA" all match a single "MPa" pattern.
        for src_text, conf in self._priority_corpora(priority_text, text):
            m = re.search(
                r"(?:SMYS|Specified\s+Minimum\s+Yield\s+Strength)"
                r"[^\d\n]{0,40}"
                + _NUM + r"\s*(?:MPa|N/mm[²2])",
                src_text,
                re.IGNORECASE,
            )
            if m:
                v = _to_float(m.group(1))
                if v and 100 <= v <= 1000:
                    self._set(md, "smys_mpa", v, conf, f"SMYS = {v} MPa")
                    break

        # ----- MAOP (kg/cm²). May also be written kgf/cm² or kgcm2.
        #       Same dual-form treatment as length: unit may sit in
        #       parens before the colon ("MAOP (kg/cm²) : 98") or
        #       trail the value ("MAOP = 98 kg/cm²").
        maop_labels = (
            r"(?:MAOP|Maximum\s+Allowable\s+Operating\s+Pressure)"
        )
        maop_unit = r"kg\s*f?\s*/?\s*cm\s*[²2]|kg/cm2|kgcm2"
        for src_text, conf in self._priority_corpora(priority_text, text):
            m = re.search(
                maop_labels + r"\s*\(\s*(?:" + maop_unit + r")\s*\)\s*[:\=\-]?\s*"
                + _NUM,
                src_text,
                re.IGNORECASE,
            )
            if not m:
                m = re.search(
                    maop_labels + r"\s*[:\=\-]?\s*"
                    + _NUM + r"\s*(?:" + maop_unit + r")",
                    src_text,
                    re.IGNORECASE,
                )
            if m:
                v = _to_float(m.group(1))
                if v and 5 <= v <= 250:
                    self._set(md, "maop_kgcm2", v, conf,
                              f"MAOP = {v} kg/cm²")
                    break

        # ----- Design factor (Fd). Usually a single decimal in [0.40, 0.90].
        for src_text, conf in self._priority_corpora(priority_text, text):
            m = re.search(
                r"(?:Design\s+Factor|\bFd\b|F\s*\(?d\)?)\s*"
                r"[:\=\-]?\s*"
                + _NUM,
                src_text,
            )
            if m:
                v = _to_float(m.group(1))
                if v and 0.40 <= v <= 0.90:
                    self._set(md, "design_factor", v, conf,
                              f"Design factor = {v}")
                    break

    # ------------------------------------------------------------ run-2 date

    def _extract_run2_date(
        self, md: ExtractedMetadata, pages: list[str], text: str,
    ) -> None:
        """Run-2 (the newer / report-driving inspection) date or year.

        Tries patterns in order of specificity:
          1. "Inspection Date: 15 March 2023"   — synthetic / explicit label
          2. "Conducted on 18th August, 2023"   — Abu Road real PDF
          3. "Date of Inspection: 15-03-2023"
          4. "Current inspection 2023"          — year only, fallback
        Whenever a full date is found, normalise it to ISO (YYYY-MM-DD)
        so the GUI can plug it straight into a QDateEdit.
        """

        # ----- Pass 1: explicit "Inspection Date:" label.
        m = re.search(
            r"(?:Inspection\s+Date|Date\s+of\s+Inspection|Survey\s+Date)\s*"
            r"[:\-]?\s*"
            r"(\d{1,2}[\-/\s][A-Za-z]{3,9}[\-/\s]\d{2,4}"
            r"|\d{4}[\-/]\d{1,2}[\-/]\d{1,2}"
            r"|\d{1,2}[\-/]\d{1,2}[\-/]\d{2,4}"
            r"|[A-Za-z]{3,9}[\-/\s]\d{2,4})",
            text,
        )
        if m:
            self._record_run2_date(md, m.group(1), CONFIDENCE_DIRECT)
            return

        # ----- Pass 2: "Conducted on 18th August, 2023" (Abu Road).
        # Athena/NGP cover pages name the inspection date this way when
        # there's no explicit "Inspection Date:" line. The ordinal
        # suffix ("18th") is optional; the comma between day-month and
        # year is also optional.
        month_alt = "|".join(_MONTHS)
        m = re.search(
            r"Conducted\s+on\s+"
            r"(\d{1,2}(?:st|nd|rd|th)?\s+(?:" + month_alt + r")"
            r"\s*,?\s*\d{4})",
            text,
            re.IGNORECASE,
        )
        if m:
            self._record_run2_date(md, m.group(1), CONFIDENCE_DIRECT)
            return

        # ----- Pass 3: "Current inspection 2023" — year only.
        m = re.search(
            r"Current\s+inspection\s+(\d{4})",
            text,
            re.IGNORECASE,
        )
        if m:
            yr = int(m.group(1))
            if 1990 <= yr <= 2099:
                self._set(md, "run2_inspection_year", yr,
                          CONFIDENCE_DIRECT,
                          f"Run-2 year from 'Current inspection {yr}'")
                self._set(md, "run2_inspection_date_str", f"{yr}-01-01",
                          CONFIDENCE_INFERRED,
                          "Run-2 date defaulted to Jan 1 of inspection year.")
            return

        # ----- Pass 4: any "report year / inspection year".
        m = re.search(
            r"(?:Inspection|Survey|Report)\s+(?:Year|of)?\s*[:\-]?\s*"
            + _INT,
            text,
            re.IGNORECASE,
        )
        if m:
            yr = int(m.group(1))
            if 1990 <= yr <= 2099:
                self._set(md, "run2_inspection_year", yr,
                          CONFIDENCE_PATTERN,
                          f"Run-2 year inferred = {yr}")

    def _record_run2_date(
        self, md: ExtractedMetadata, raw: str, confidence: float,
    ) -> None:
        """Store the raw date string + ISO-normalised string + year."""
        raw = re.sub(r"\s+", " ", raw).strip().rstrip(",.")
        iso = _parse_date_to_iso(raw)
        # Store the ISO form when we can parse it, otherwise the raw.
        date_str = iso if iso else raw
        self._set(md, "run2_inspection_date_str", date_str,
                  confidence, f"Run-2 date = {date_str}")
        # Year — pull from ISO if we have it, else regex the raw.
        if iso:
            yr = int(iso[:4])
        else:
            ym = re.search(r"(\d{4})", raw)
            yr = int(ym.group(1)) if ym else 0
        if 1990 <= yr <= 2099:
            self._set(md, "run2_inspection_year", yr,
                      CONFIDENCE_INFERRED)

    # ------------------------------------------------------------ run-1 date

    def _extract_run1_date(
        self, md: ExtractedMetadata, pages: list[str], text: str,
    ) -> None:
        """Run-1 (PREVIOUS inspection) date from page-6 Pipeline-Data table.

        The Athena / NGP Final Report's Pipeline Data table always has
        a "Previous inspections" row. Three forms seen in real reports:

            "Previous inspections  15 May 2019"   ← full date, high conf
            "Previous inspections  May 2019"      ← month-year, mid conf
            "Previous inspections  2019"          ← year only, low conf

        Defaults applied when partial dates are seen:
            month-year → day 15 (mid-month)
            year only  → 1 July (mid-year)
        ...both so the GUI's QDateEdit gets a non-zero value the user
        can adjust with one click.

        Without this, users had to type the Run-1 date manually and
        sometimes typo'd it — leading to the negative-years_between
        bug we hit on the Abu Road smoke test.
        """
        month_alt = "|".join(_MONTHS)

        # ----- Pattern 1: full date (1.0 confidence).
        # "Previous inspections 15 May 2019" / "...15th May 2019"
        m = re.search(
            r"Previous\s+inspections?\s+"
            r"(\d{1,2}(?:st|nd|rd|th)?\s+(?:" + month_alt + r")\s+\d{4})",
            text, re.IGNORECASE,
        )
        if m:
            self._record_run1_date(md, m.group(1), CONFIDENCE_DIRECT)
            return

        # ----- Pattern 2: "Previous ILI: 15 May 2019" — alt phrasing.
        m = re.search(
            r"Previous\s+ILI\s*[:\-]?\s*"
            r"(\d{1,2}(?:st|nd|rd|th)?\s+(?:" + month_alt + r")\s+\d{4})",
            text, re.IGNORECASE,
        )
        if m:
            self._record_run1_date(md, m.group(1), CONFIDENCE_DIRECT)
            return

        # ----- Pattern 3: month-year only (0.8 confidence).
        # "Previous inspections May 2019" → default day 15.
        m = re.search(
            r"Previous\s+inspections?\s+"
            r"((?:" + month_alt + r")\s+\d{4})",
            text, re.IGNORECASE,
        )
        if m:
            self._record_run1_date(md, m.group(1), 0.8, default_day=15)
            return

        # Same month-year shape under "Previous ILI:" / "Baseline inspection:".
        for label in (r"Previous\s+ILI", r"Baseline\s+inspection"):
            m = re.search(
                label + r"\s*[:\-]?\s*((?:" + month_alt + r")\s+\d{4})",
                text, re.IGNORECASE,
            )
            if m:
                self._record_run1_date(md, m.group(1), 0.8, default_day=15)
                return

        # ----- Pattern 4: year-only (0.5 confidence).
        # "Previous inspections 2017" → default 1 July.
        m = re.search(
            r"Previous\s+inspections?\s+(\d{4})",
            text, re.IGNORECASE,
        )
        if m:
            year = int(m.group(1))
            if 1990 <= year <= 2099:
                iso = f"{year}-07-01"
                self._set(md, "run1_inspection_date_str", iso,
                          CONFIDENCE_INFERRED,
                          f"Run-1 date = {iso} (year-only — defaulted "
                          "to 1 July; please verify).")
                self._set(md, "run1_inspection_year", year, CONFIDENCE_INFERRED)
                return

        # Same year-only shape for "Baseline inspection 2017".
        m = re.search(
            r"Baseline\s+inspection\s*[:\-]?\s*(\d{4})",
            text, re.IGNORECASE,
        )
        if m:
            year = int(m.group(1))
            if 1990 <= year <= 2099:
                iso = f"{year}-07-01"
                self._set(md, "run1_inspection_date_str", iso,
                          CONFIDENCE_INFERRED,
                          f"Run-1 date = {iso} (year-only — defaulted "
                          "to 1 July; please verify).")
                self._set(md, "run1_inspection_year", year, CONFIDENCE_INFERRED)
                return

        # Nothing found — leave None, log a guidance note for the GUI.
        md.extraction_notes.append(
            "No previous-inspection date found in PDF — enter the "
            "Run-1 inspection date manually."
        )

    def _record_run1_date(
        self,
        md: ExtractedMetadata,
        raw: str,
        confidence: float,
        default_day: int | None = None,
    ) -> None:
        """Store Run-1 date as ISO + year. Mirrors _record_run2_date.

        When `default_day` is given, the raw is treated as month-year
        (e.g. "May 2019"); we prepend a day-of-month and run the same
        ISO parser as Run-2.
        """
        raw = re.sub(r"\s+", " ", raw).strip().rstrip(",.")
        if default_day is not None:
            # "May 2019" → "15 May 2019" so _parse_date_to_iso recognises it.
            iso = _parse_date_to_iso(f"{default_day} {raw}")
            note_suffix = " (month-year — defaulted to mid-month)."
        else:
            iso = _parse_date_to_iso(raw)
            note_suffix = ""
        if iso:
            self._set(md, "run1_inspection_date_str", iso, confidence,
                      f"Run-1 date = {iso}{note_suffix}")
            yr = int(iso[:4])
            if 1990 <= yr <= 2099:
                self._set(md, "run1_inspection_year", yr, CONFIDENCE_INFERRED)
        else:
            # Couldn't ISO-normalise but we have a raw — keep it so
            # the GUI shows SOMETHING.
            self._set(md, "run1_inspection_date_str", raw, confidence,
                      f"Run-1 date raw = {raw}")
            ym = re.search(r"(\d{4})", raw)
            if ym:
                yr = int(ym.group(1))
                if 1990 <= yr <= 2099:
                    self._set(md, "run1_inspection_year", yr,
                              CONFIDENCE_INFERRED)

    # -------------------------------------------------------------- internals

    @staticmethod
    def _page_text(pages: list[str], page_num: int) -> str:
        """Return text for a 1-indexed page, or empty if out of range."""
        idx = page_num - 1
        if 0 <= idx < len(pages):
            return pages[idx]
        return ""

    @staticmethod
    def _priority_corpora(priority: str, full: str):
        """Yield (text, confidence) — priority page first, then full doc."""
        if priority:
            yield priority, CONFIDENCE_DIRECT
        yield full, CONFIDENCE_PATTERN


__all__ = ["ExtractedMetadata", "VendorReportParser"]
