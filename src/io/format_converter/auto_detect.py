"""Best-effort profile proposer.

Given a vendor Excel file, scan its sheets for the most likely "Defects"-
equivalent, then fuzzy-match each header against the synonyms in
``config/column_synonyms.yaml``. The result is a draft :class:`VendorProfile`
the user can review/edit in the GUI before converting.

This is intentionally a *draft generator*, not a perfect parser. The output
:class:`ProfileProposal` exposes per-field confidence so the GUI can flag
low-confidence guesses for explicit user confirmation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from .converter import _excel_engine_for, _norm
from .profile import CANONICAL_FIELDS, VendorProfile


# ---------------------------------------------------------------------------
# Heuristics for sheet & header detection
# ---------------------------------------------------------------------------

# Sheet-name heuristics — case-insensitive substring match.
_PREFERRED_SHEET_KEYWORDS = (
    "defect", "metal loss", "severity", "anomaly", "feature", "tally",
)
# Sheets to skip outright.
_SKIP_SHEET_KEYWORDS = (
    "weld", "casing", "wall thickness", "reference point", "bend",
    "installation", "adjacent", "bdv", "valve",
)

# How many header-row candidates to score in each sheet.
_HEADER_SCAN_ROWS = 12
# Minimum canonical hits a sheet must show to be considered a defect sheet.
_MIN_HITS = 3


@dataclass
class ProfileProposal:
    """Draft profile + per-field confidence scores for GUI display.

    ``confidence`` values:
      * ``1.00`` — exact match against an explicit synonym in
        ``column_synonyms.yaml``.
      * ``0.50`` — fuzzy match via numeric-pattern + heuristic.
      * absent — field not detected; the user must fill it in.
    """
    profile: VendorProfile
    confidence: dict[str, float] = field(default_factory=dict)
    unmapped_source_columns: list[str] = field(default_factory=list)
    sheet_scores: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Synonym loading
# ---------------------------------------------------------------------------

def _default_synonyms_path() -> Path:
    """Locate ``config/column_synonyms.yaml`` relative to project root."""
    return Path(__file__).resolve().parents[3] / "config" / "column_synonyms.yaml"


def _load_synonyms(path: Path | None = None) -> dict[str, list[str]]:
    """Return ``{canonical_field: [normalised_synonym, …]}`` for matching."""
    p = path or _default_synonyms_path()
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    out: dict[str, list[str]] = {}
    for canon in CANONICAL_FIELDS:
        entry = raw.get(canon, {})
        # depth_mm has no entry in synonyms (depth is depth_pct_wt there);
        # we'll detect it indirectly via header hints.
        syns = entry.get("synonyms", []) if isinstance(entry, dict) else []
        out[canon] = [_norm(s) for s in syns]
    return out


def _load_value_normalisations(path: Path | None = None) -> dict[str, dict[str, str]]:
    """Return value-normalisation dicts keyed by canonical field.

    Inverts the YAML layout (which is keyed by canonical *value* with
    lists of source values) so we can look up ``"I"`` -> ``"internal"``.
    """
    p = path or _default_synonyms_path()
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    norms = raw.get("value_normalisations") or {}
    out: dict[str, dict[str, str]] = {}
    for canon_field, by_canonical_value in norms.items():
        if not isinstance(by_canonical_value, dict):
            continue
        flat: dict[str, str] = {}
        for canon_value, source_values in by_canonical_value.items():
            if not isinstance(source_values, list):
                continue
            for src in source_values:
                flat[_norm(src)] = canon_value
        out[canon_field] = flat
    return out


# ---------------------------------------------------------------------------
# Unit + format heuristics
# ---------------------------------------------------------------------------

_UNIT_TOKEN_RE = re.compile(
    r"[\[\(,]\s*(?P<unit>mm|m|km|ft|cm|in|inch|%|pct|hh:?mm|degrees|deg|rad)\s*[\]\)]?",
    re.IGNORECASE,
)


def _guess_unit_from_header(header: str, default: str) -> str:
    """Inspect the bracketed/parenthesised unit suffix in a header.

    e.g. ``"Length, mm"`` -> ``"mm"``, ``"Distance [ft]"`` -> ``"ft"``.
    """
    if not header:
        return default
    m = _UNIT_TOKEN_RE.search(str(header))
    if not m:
        return default
    return m.group("unit").lower()


def _guess_units(matched_columns: dict[str, str]) -> dict[str, str]:
    """Populate a ``unit_conventions`` dict from header-suffix hints."""
    units: dict[str, str] = {}

    def _set(unit_key: str, header: str | None, default: str) -> None:
        if not header:
            return
        units[unit_key] = _guess_unit_from_header(header, default)

    _set("chainage",            matched_columns.get("abs_distance_m"), "m")
    _set("upstream_weld_dist",  matched_columns.get("upstream_weld_dist_m"), "m")
    _set("wall_thickness",      matched_columns.get("wt_mm"), "mm")
    _set("length",              matched_columns.get("length_mm"), "mm")
    _set("width",               matched_columns.get("width_mm"), "mm")
    _set("altitude",            matched_columns.get("altitude_m"), "m")

    # Depth needs a touch more nuance — the header may say "%", "mm", or
    # be silent.
    depth_header = matched_columns.get("depth_pct_wt")
    if depth_header:
        u = _guess_unit_from_header(depth_header, "%")
        units["depth"] = u if u in ("%", "pct", "mm") else "%"

    # Clock — common variants.
    clk_header = matched_columns.get("clock_position")
    if clk_header:
        h = _norm(clk_header)
        if "hh" in h or "h:min" in h or "h min" in h or "min" in h:
            units["clock"] = "hh:mm"
        elif "deg" in h:
            units["clock"] = "degrees"
        elif "rad" in h:
            units["clock"] = "radians"
        else:
            units["clock"] = "hh:mm"

    return units


# ---------------------------------------------------------------------------
# Sheet selection
# ---------------------------------------------------------------------------

def _read_workbook(path: Path) -> dict[str, pd.DataFrame]:
    """Return all sheets as DataFrames, headers NOT yet promoted."""
    engine = _excel_engine_for(path)
    book = pd.read_excel(path, sheet_name=None, header=None, engine=engine)
    return book


def _sheet_priority(sheet_name: str) -> int:
    """Higher = better. -1 = skip."""
    n = sheet_name.lower()
    for skip in _SKIP_SHEET_KEYWORDS:
        if skip in n:
            return -1
    for i, kw in enumerate(_PREFERRED_SHEET_KEYWORDS):
        if kw in n:
            return 100 - i
    return 1


def propose_mappings_for_dataframe(
    df: pd.DataFrame,
    *,
    synonyms_path: Path | None = None,
) -> dict[str, str]:
    """Match a single DataFrame's headers against the canonical synonyms.

    Used by the GUI's pipe-registry section (which already knows which
    sheet + header row it's reading and just wants the column-name
    matches without re-opening the file). Returns ``{canonical: header}``.
    """
    synonyms = _load_synonyms(synonyms_path)
    headers = [c if c is not None else "" for c in df.columns]
    _hits, mapping = _score_header_row(list(headers), synonyms)
    return mapping


def _score_header_row(
    row_values: list[Any], synonyms: dict[str, list[str]],
) -> tuple[int, dict[str, str]]:
    """Count how many canonical fields this row's headers cover.

    Returns ``(hits, {canonical: source_header})``.
    """
    normalised = [(_norm(v), str(v) if v is not None else "")
                  for v in row_values]
    hits: dict[str, str] = {}
    for canon, syn_list in synonyms.items():
        for ncell, raw in normalised:
            if not ncell:
                continue
            if ncell in syn_list:
                hits[canon] = raw
                break
    return len(hits), hits


def _find_best_sheet_and_header(
    book: dict[str, pd.DataFrame], synonyms: dict[str, list[str]],
) -> tuple[str | None, int, dict[str, str], dict[str, int]]:
    """Return ``(sheet, header_row, {canonical→header}, {sheet→score})``.

    Picks the sheet whose best header row has the highest canonical-hit
    count, with a tiebreak on sheet-name priority.
    """
    sheet_scores: dict[str, int] = {}
    best: tuple[int, int, str | None, int, dict[str, str]] = (
        -1, -1, None, 0, {},
    )    # (hits, sheet_priority, sheet, header_row, mapping)

    for sheet, df in book.items():
        prio = _sheet_priority(sheet)
        if prio < 0:
            sheet_scores[sheet] = -1
            continue
        local_best = (0, 0, {})       # (hits, header_row, mapping)
        scan_rows = min(_HEADER_SCAN_ROWS, len(df))
        for r in range(scan_rows):
            row_vals = df.iloc[r].tolist()
            hits, mapping = _score_header_row(row_vals, synonyms)
            if hits > local_best[0]:
                local_best = (hits, r, mapping)
        sheet_scores[sheet] = local_best[0]
        candidate = (local_best[0], prio, sheet, local_best[1], local_best[2])
        if candidate > best:
            best = candidate

    hits, _prio, sheet, header_row, mapping = best
    if hits < _MIN_HITS:
        return None, 0, {}, sheet_scores
    return sheet, header_row, mapping, sheet_scores


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def propose_profile(
    file_path: str | Path,
    *,
    vendor_name_hint: str = "",
    synonyms_path: str | Path | None = None,
) -> ProfileProposal:
    """Read a vendor file, propose a :class:`VendorProfile`, return a draft.

    The proposal includes:

      * the picked sheet name + header row (or ``None`` if no plausible
        defect sheet was found),
      * a ``column_mappings`` dict for the canonical fields the
        synonym engine recognised,
      * a ``unit_conventions`` dict guessed from header-suffix hints,
      * a draft ``value_normalizations`` block for ``surface`` derived
        from the existing global table (so vendors that ship "I"/"E"
        get caught automatically),
      * per-field ``confidence`` (1.0 for exact synonym hits today),
      * the list of vendor columns we *couldn't* place, for the GUI's
        "drop these onto a field" workflow.

    Args:
        file_path: Path to the vendor Excel file (.xlsx / .xls).
        vendor_name_hint: Optional label to seed ``profile.vendor_name``.
        synonyms_path: Override path to ``column_synonyms.yaml`` (for tests).
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Source file not found: {path}")

    syn_path = Path(synonyms_path) if synonyms_path else None
    synonyms = _load_synonyms(syn_path)
    value_norms = _load_value_normalisations(syn_path)

    book = _read_workbook(path)
    sheet, header_row, mapping, sheet_scores = _find_best_sheet_and_header(
        book, synonyms,
    )

    profile = VendorProfile(
        vendor_name=vendor_name_hint or f"Auto-detected ({path.name})",
        sheet_name=sheet,
        header_row=header_row,
        column_mappings=mapping,
        unit_conventions=_guess_units(mapping),
        value_normalizations={},
        notes=(
            "Draft profile generated by auto-detect. "
            "Review every field; low-confidence guesses are flagged."
        ),
    )

    # Surface-value normalisation: ship the standard table so vendors who
    # use "I"/"E" or "Int"/"Ext" don't surprise the user.
    if "surface" in value_norms and "surface" in mapping:
        profile.value_normalizations["surface"] = value_norms["surface"]

    # Feature-identification normalisation likewise — gives the user a
    # head start on mapping free-text descriptions to POF codes.
    if "feature_identification" in value_norms and (
        "feature_identification" in mapping
    ):
        profile.value_normalizations["feature_identification"] = (
            value_norms["feature_identification"]
        )

    confidence = {canon: 1.0 for canon in mapping}

    # Identify the vendor headers we *couldn't* place — useful for the
    # GUI's drop-target workflow.
    unmapped: list[str] = []
    if sheet is not None:
        df_raw = book[sheet]
        if header_row < len(df_raw):
            row = df_raw.iloc[header_row].tolist()
            mapped_headers = {str(v) for v in mapping.values()}
            for cell in row:
                if cell is None:
                    continue
                raw = str(cell).strip()
                if not raw:
                    continue
                if raw in mapped_headers:
                    continue
                unmapped.append(raw)

    notes: list[str] = []
    if sheet is None:
        notes.append(
            "No plausible defect sheet found. Set sheet_name + header_row "
            "manually and try again."
        )
    elif len(mapping) < 6:
        notes.append(
            f"Only matched {len(mapping)} canonical fields — the file is "
            "probably non-standard. Fill in the rest by hand."
        )

    return ProfileProposal(
        profile=profile,
        confidence=confidence,
        unmapped_source_columns=unmapped,
        sheet_scores=sheet_scores,
        notes=notes,
    )
