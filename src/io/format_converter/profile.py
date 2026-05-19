"""VendorProfile dataclass — the recipe for mapping a vendor's sheet to NGP.

A profile encodes everything the :class:`FormatConverter` needs to know
about *one specific vendor format*:

  * Which sheet + which header row to read.
  * Which source column name carries each canonical field.
  * What units the source uses (so we can convert chainage km→m, etc.).
  * Per-field source→canonical value normalisations (e.g. surface
    "I"/"E" → INTERNAL/EXTERNAL when the vendor doesn't use POF codes).

Profiles are JSON-serialisable so they can live as ``profiles/*.json``
on disk, ship with the tool, and be hand-edited by the user via the GUI
(future prompt).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Canonical field catalogue
# ---------------------------------------------------------------------------
#
# Canonical names match the keys used by ``config/column_synonyms.yaml`` and
# ``src/io/ili_reader.py``. The ``ALIASES`` map accepts a couple of
# friendlier names from the public API (e.g. the user prompt called the
# defect ID ``feature_id`` and clock position ``clock_orientation``) and
# rewrites them to the reader's canonical spelling so a profile written
# against either name "just works".

CANONICAL_FIELDS: tuple[str, ...] = (
    # Identifier
    "anomaly_id",
    # Geometry
    "abs_distance_m",
    "upstream_weld_dist_m",
    "joint_number",
    "joint_length_m",
    "wt_mm",
    # Defect dimensions
    "depth_pct_wt",
    "depth_mm",          # depth in mm — converted to %WT during transform
    "length_mm",
    "width_mm",
    # Orientation
    "clock_position",
    # Categorical
    "surface",
    "feature_identification",
    "dimension_class",
    # Geo
    "latitude",
    "longitude",
    "altitude_m",
    # Free text
    "description",
)

# Field that the reader will refuse to parse a row without. Mirrors the
# ``required`` list in ``src/io/ili_reader.py`` (depth can be supplied
# either as %WT or mm — the converter resolves to %WT before write).
REQUIRED_CANONICAL_FIELDS: tuple[str, ...] = (
    "abs_distance_m",
    "joint_number",
    "wt_mm",
    "length_mm",
    "width_mm",
    "surface",
)

# Public aliases accepted in column_mappings/value_normalizations. The
# converter rewrites these before any downstream lookup.
ALIASES: dict[str, str] = {
    "feature_id": "anomaly_id",
    "clock_orientation": "clock_position",
}

# Either of these resolves the depth requirement.
_DEPTH_EQUIVALENT = ("depth_pct_wt", "depth_mm")


def _canonicalise(field_name: str) -> str:
    """Return the reader-canonical spelling for a possibly-aliased field."""
    return ALIASES.get(field_name, field_name)


# ---------------------------------------------------------------------------
# VendorProfile dataclass
# ---------------------------------------------------------------------------

@dataclass
class VendorProfile:
    """One vendor's recipe for translating a pipe-tally file to NGP format.

    Attributes:
        vendor_name: Free-text label (``"Rosen 2018"``, ``"NDT Global EmatPlus"``).
        sheet_name: Which sheet to read. ``None`` = first sheet found.
        header_row: Zero-indexed row where the column headers live.
        column_mappings: ``{canonical_field: source_column_name}``. Canonical
            names must come from :data:`CANONICAL_FIELDS` (aliases accepted).
        unit_conventions: Units used in the SOURCE file. Recognised keys:
            ``chainage``, ``depth``, ``clock``, ``length``, ``width``,
            ``wall_thickness``, ``upstream_weld_dist``. Sensible defaults
            apply for omitted keys (everything in SI: m / mm / hh:mm).
        value_normalizations: ``{canonical_field: {source_value: canonical_value}}``
            for categorical fields. Source values are matched
            case-insensitively after whitespace squashing.
        notes: Free text for human-readable provenance ("Drafted from
            Rosen public spec, 2024-09").
        pipe_sheet_name: Optional name of a secondary "Pipeline Tally"
            sheet carrying the FULL joint registry (welds + every joint,
            with or without defects). The downstream reader uses this to
            build a complete joint sequence for alignment; without it,
            only joints that have ≥1 defect end up in the registry, and
            joint-alignment degrades badly. Leave as ``None`` for vendors
            that ship single-sheet files.
        pipe_header_row: Header row for the pipe sheet (zero-indexed).
        pipe_column_mappings: Per-field source columns for the pipe sheet.
            At minimum, ``joint_number`` and one of ``joint_length_m`` or
            ``abs_distance_m`` should be mapped; ``wt_mm``, ``latitude``,
            ``longitude`` are helpful but optional.
    """
    vendor_name: str = ""
    sheet_name: str | None = None
    header_row: int = 0
    column_mappings: dict[str, str] = field(default_factory=dict)
    unit_conventions: dict[str, str] = field(default_factory=dict)
    value_normalizations: dict[str, dict[str, str]] = field(default_factory=dict)
    notes: str = ""

    # Optional pipe-registry sheet pass-through
    pipe_sheet_name: str | None = None
    pipe_header_row: int = 0
    pipe_column_mappings: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ I/O

    def save_to_json(self, path: str | Path) -> Path:
        """Write the profile to a JSON file. Returns the resolved path."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False, sort_keys=False)
        return p

    @classmethod
    def load_from_json(cls, path: str | Path) -> "VendorProfile":
        """Load a profile from JSON. Unknown keys are ignored."""
        p = Path(path)
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Profile JSON must be an object, got {type(data).__name__}")
        # Defensive: drop unrecognised top-level keys so a future schema
        # rev doesn't break older profiles.
        known = {
            "vendor_name", "sheet_name", "header_row",
            "column_mappings", "unit_conventions",
            "value_normalizations", "notes",
            "pipe_sheet_name", "pipe_header_row", "pipe_column_mappings",
        }
        clean = {k: v for k, v in data.items() if k in known}
        return cls(**clean)

    # --------------------------------------------------------------- helpers

    def normalised_mappings(self) -> dict[str, str]:
        """Return column_mappings with aliases rewritten to canonical names.

        Raises ValueError if an entry references a name that is neither
        canonical nor aliased.
        """
        out: dict[str, str] = {}
        for fname, src in self.column_mappings.items():
            canon = _canonicalise(fname)
            if canon not in CANONICAL_FIELDS:
                raise ValueError(
                    f"column_mappings entry {fname!r} is not a recognised "
                    f"canonical field (allowed: {', '.join(CANONICAL_FIELDS)})"
                )
            out[canon] = src
        return out

    def normalised_pipe_mappings(self) -> dict[str, str]:
        """As :meth:`normalised_mappings` but for the pipe sheet."""
        out: dict[str, str] = {}
        for fname, src in self.pipe_column_mappings.items():
            canon = _canonicalise(fname)
            if canon not in CANONICAL_FIELDS:
                raise ValueError(
                    f"pipe_column_mappings entry {fname!r} is not a "
                    f"recognised canonical field"
                )
            out[canon] = src
        return out

    def normalised_value_normalizations(self) -> dict[str, dict[str, str]]:
        """As :meth:`normalised_mappings` but for ``value_normalizations``."""
        out: dict[str, dict[str, str]] = {}
        for fname, mapping in self.value_normalizations.items():
            canon = _canonicalise(fname)
            if canon not in CANONICAL_FIELDS:
                raise ValueError(
                    f"value_normalizations entry {fname!r} is not a "
                    f"recognised canonical field"
                )
            if not isinstance(mapping, dict):
                raise ValueError(
                    f"value_normalizations[{fname!r}] must be a dict, "
                    f"got {type(mapping).__name__}"
                )
            out[canon] = {str(k): str(v) for k, v in mapping.items()}
        return out

    # --------------------------------------------------------------- validate

    def validate(self) -> list[str]:
        """Return a list of human-readable problems with this profile.

        Empty list = profile is good to use. A non-empty list does NOT
        prevent loading — the GUI uses this for "show warnings, let user
        fix" rather than "refuse to proceed".
        """
        problems: list[str] = []

        if not self.vendor_name.strip():
            problems.append("vendor_name is empty")

        if self.header_row < 0:
            problems.append(f"header_row must be >= 0 (got {self.header_row})")

        # Catch typo / unknown canonical names early.
        try:
            mappings = self.normalised_mappings()
        except ValueError as e:
            problems.append(str(e))
            mappings = {}

        try:
            self.normalised_value_normalizations()
        except ValueError as e:
            problems.append(str(e))

        # Required fields — every required canonical must be present.
        # Depth has two equivalent forms (pct vs mm) and is satisfied by
        # either.
        missing: list[str] = []
        for f in REQUIRED_CANONICAL_FIELDS:
            if f not in mappings:
                missing.append(f)

        depth_present = (
            "depth_pct_wt" in mappings or "depth_mm" in mappings
        )
        if not depth_present:
            missing.append("depth_pct_wt | depth_mm  (one is required)")

        # Need wt_mm if depth is in mm (because we have to convert to %WT).
        if "depth_mm" in mappings and "wt_mm" not in mappings:
            problems.append(
                "depth_mm given but wt_mm not mapped — need wt_mm to "
                "convert depth to %WT"
            )

        if missing:
            problems.append(
                "Missing required canonical fields: " + ", ".join(missing)
            )

        # Unit-convention sanity (warn on unrecognised keys; don't fail).
        recognised_unit_keys = {
            "chainage", "depth", "clock", "length", "width",
            "wall_thickness", "upstream_weld_dist", "altitude",
        }
        unknown_units = [
            k for k in self.unit_conventions
            if k not in recognised_unit_keys
        ]
        if unknown_units:
            problems.append(
                "Unknown unit_conventions keys (will be ignored): "
                + ", ".join(unknown_units)
            )

        return problems

    def is_valid(self) -> bool:
        """Convenience: ``True`` if :meth:`validate` returns an empty list."""
        return not self.validate()
