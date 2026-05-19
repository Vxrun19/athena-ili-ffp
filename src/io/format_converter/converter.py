"""FormatConverter — vendor-format → NGP-format Excel transformation.

The class is intentionally a thin orchestrator over three pure-ish steps:

  1. **read_source(path)** — open the workbook, pick the right sheet,
     promote the header row, return the raw vendor frame.
  2. **transform(df)** — rename columns to the NGP canonical names,
     apply unit conversions, apply value normalisations.
  3. **write_ngp_format(df, path)** — write to a single "Defects" sheet
     in an .xlsx file that the existing :class:`~src.io.ili_reader.ILIReader`
     can ingest unchanged.

:meth:`convert` chains all three.

Output column names are drawn from :data:`NGP_OUTPUT_COLUMNS` — every
entry is an actual synonym listed in ``config/column_synonyms.yaml`` so
the reader recognises them without any extra glue.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .profile import (
    CANONICAL_FIELDS,
    REQUIRED_CANONICAL_FIELDS,
    VendorProfile,
)
from .unit_conversions import (
    chainage_to_m,
    clock_to_hh_mm,
    depth_to_pct_wt,
)


# ---------------------------------------------------------------------------
# NGP output column naming
# ---------------------------------------------------------------------------
#
# The reader's column resolution is forgiving (case-insensitive,
# punctuation-tolerant). We still write the cleanest, most-NGP-2023 form
# of each name — every value here is a verbatim entry from
# ``config/column_synonyms.yaml`` so we can't drift by accident.

NGP_OUTPUT_COLUMNS: dict[str, str] = {
    "anomaly_id":             "Anomaly ID",
    "abs_distance_m":         "Absolute Distance, m",
    "upstream_weld_dist_m":   "Upstream Weld Distance",
    "joint_number":           "Joint Number",
    "joint_length_m":         "Joint Length, m",
    "wt_mm":                  "WT, mm",
    "depth_pct_wt":           "Depth, %WT",
    "length_mm":              "Length, mm",
    "width_mm":               "Width, mm",
    "clock_position":         "Clock Position",
    "surface":                "Surface",
    "feature_identification": "POF Acronym",
    "dimension_class":        "Dimension Classification",
    "latitude":               "Latitude",
    "longitude":              "Longitude",
    "altitude_m":             "Altitude, m",
    "description":            "Description",
}

# Order of columns in the output sheet. Keeps the file scannable.
_OUTPUT_ORDER = (
    "anomaly_id",
    "abs_distance_m",
    "upstream_weld_dist_m",
    "joint_number",
    "joint_length_m",
    "wt_mm",
    "depth_pct_wt",
    "length_mm",
    "width_mm",
    "clock_position",
    "surface",
    "feature_identification",
    "dimension_class",
    "description",
    "latitude",
    "longitude",
    "altitude_m",
)

# Defaults applied when the profile leaves a unit blank.
_DEFAULT_UNITS = {
    "chainage": "m",
    "upstream_weld_dist": "m",
    "depth": "%",          # Depth is the most common surprise — many
                           # vendors give mm; profiles must say so.
    "clock": "hh:mm",
    "length": "mm",
    "width": "mm",
    "wall_thickness": "mm",
    "altitude": "m",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    """Whitespace + punctuation-tolerant key for value normalisation lookup.

    Mirrors the same convention used by ``src/io/ili_reader.py`` so any
    value the user enters in their profile matches the way the reader
    will compare strings downstream.
    """
    if s is None:
        return ""
    return re.sub(r"[\s_\-/.,()\[\]{}'\"`²°*:%]+", " ", str(s)).strip().lower()


def _apply_value_normalisations(
    series: pd.Series, mapping: dict[str, str]
) -> pd.Series:
    """Map vendor-specific source values to NGP canonical values.

    Keys/values are compared after :func:`_norm`. Unmatched values pass
    through unchanged.
    """
    if not mapping:
        return series
    norm_map = {_norm(k): str(v) for k, v in mapping.items()}

    def _convert(v: Any) -> Any:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return v
        key = _norm(v)
        return norm_map.get(key, v)

    return series.map(_convert)


def _excel_engine_for(path: Path) -> str | None:
    """Pick the right pandas/openpyxl engine for the file. None = pandas-default."""
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        return "openpyxl"
    if ext == ".xls":
        # xlrd 2.x is XLS-only; magic-byte mismatches will surface as
        # exceptions, which is fine — the user can rename the file.
        return "xlrd"
    return None      # pandas falls back to its default sniffing


# ---------------------------------------------------------------------------
# FormatConverter
# ---------------------------------------------------------------------------

class FormatConverter:
    """Translate a vendor ILI file into the NGP layout the reader consumes.

    Usage::

        profile = VendorProfile.load_from_json("profiles/rosen_2018.json")
        FormatConverter(profile).convert("rosen.xlsx", "converted_ngp.xlsx")
    """

    def __init__(self, profile: VendorProfile) -> None:
        self.profile = profile
        problems = profile.validate()
        if problems:
            # We don't raise — partial profiles are useful for iterative
            # work in the GUI. The caller can inspect .profile_problems
            # and decide whether to push the convert through.
            self.profile_problems = problems
        else:
            self.profile_problems = []

    # ------------------------------------------------------------------ I/O

    def read_source(self, file_path: str | Path) -> pd.DataFrame:
        """Read the source file as a raw DataFrame.

        Uses the profile's ``sheet_name`` (or the first sheet if
        ``None``) and ``header_row``. Empty trailing columns / rows are
        dropped so the caller sees a clean frame.

        Polymorphic by file extension:

          * ``.csv`` → :func:`read_csv_with_encoding_fallback` so the
            encoding cascade (utf-8 / utf-8-sig / latin-1 / cp1252 /
            utf-16) handles vendor exports with non-ASCII characters
            in headers (e.g. Athena 2018's "Latitude [°]" in latin-1).
            The profile's ``sheet_name`` is ignored for CSV — there's
            only ever one "sheet".
          * everything else → :func:`pandas.read_excel`.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Source file not found: {path}")

        if path.suffix.lower() == ".csv":
            # CSV input — route through the encoding-fallback helper.
            # Bare pd.read_excel(path) on a .csv raises the cryptic
            # "Excel file format cannot be determined, you must
            # specify an engine manually" error that bit Prompt 34.
            from .csv_input import read_csv_with_encoding_fallback
            df = read_csv_with_encoding_fallback(
                path, header=self.profile.header_row,
            )
        else:
            engine = _excel_engine_for(path)
            sheet = self.profile.sheet_name
            # `sheet_name=None` returns a dict-of-frames; we want a
            # single frame, so fall back to sheet 0 in that case.
            sheet_arg: Any = sheet if sheet else 0
            df = pd.read_excel(
                path,
                sheet_name=sheet_arg,
                header=self.profile.header_row,
                engine=engine,
            )
            if isinstance(df, dict):                              # pragma: no cover
                df = next(iter(df.values()))

        # Drop fully empty columns (vendors often have trailing blanks)
        # and rows so the transform stage doesn't have to filter NaN
        # garbage.
        df = df.dropna(axis=1, how="all").dropna(axis=0, how="all")
        return df.reset_index(drop=True)

    # -------------------------------------------------------------- transform

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply the column mapping + units + value normalisation.

        Returns a new DataFrame with NGP-canonical column names. Source
        columns missing from the input frame are simply absent from the
        output (the reader's required-field check will catch real
        problems downstream — we don't double-validate here).
        """
        mappings = self.profile.normalised_mappings()
        value_normalisations = self.profile.normalised_value_normalizations()
        units = {**_DEFAULT_UNITS, **(self.profile.unit_conventions or {})}

        # 1) Pull each mapped source column into a per-canonical-field series.
        canonical_series: dict[str, pd.Series] = {}
        missing_sources: list[str] = []
        for canon_field, src_col in mappings.items():
            if src_col not in df.columns:
                missing_sources.append(f"{canon_field} -> {src_col!r}")
                continue
            canonical_series[canon_field] = df[src_col].copy()

        if missing_sources:
            raise KeyError(
                "Profile references source columns that aren't in the file: "
                + "; ".join(missing_sources)
                + f". Available columns: {list(df.columns)!r}"
            )

        # 2) Unit conversions.
        canonical_series = self._convert_units(canonical_series, units)

        # 3) Value normalisations (per-field source→canonical maps).
        for canon_field, vmap in value_normalisations.items():
            if canon_field in canonical_series:
                canonical_series[canon_field] = _apply_value_normalisations(
                    canonical_series[canon_field], vmap,
                )

        # 4) Assemble the output frame in a stable column order.
        out = pd.DataFrame()
        for canon in _OUTPUT_ORDER:
            if canon in canonical_series:
                out[NGP_OUTPUT_COLUMNS[canon]] = canonical_series[canon]
        # 5) Auto-generate an Anomaly ID column if the profile didn't
        #    supply one — the reader uses anomaly_id as a stable key for
        #    matching, so a missing column would force it to invent
        #    surrogates (`run_1_row42`) and waste downstream effort.
        if "anomaly_id" not in canonical_series:
            out.insert(
                0,
                NGP_OUTPUT_COLUMNS["anomaly_id"],
                [f"A-{i + 1:06d}" for i in range(len(out))],
            )

        return out.reset_index(drop=True)

    def _convert_units(
        self,
        series_by_field: dict[str, pd.Series],
        units: dict[str, str],
    ) -> dict[str, pd.Series]:
        """Apply per-field unit conversions to the assembled series dict."""

        # --- chainage / upstream weld distance ------------------------------
        for canon, unit_key in (
            ("abs_distance_m", "chainage"),
            ("upstream_weld_dist_m", "upstream_weld_dist"),
        ):
            if canon in series_by_field:
                src_unit = units.get(unit_key, "m")
                series_by_field[canon] = series_by_field[canon].map(
                    lambda v, u=src_unit: _safe(chainage_to_m, v, u),
                )

        # --- joint length / WT / length / width / altitude -----------------
        # All in mm or m — convert metres↔mm where necessary; default
        # already-canonical.
        if "joint_length_m" in series_by_field:
            unit = units.get("length", "mm")  # joint length is the odd one
            # If the user said joint_length is in mm, divide; in m, pass through.
            if unit in ("mm", "millimetre", "millimeters"):
                # Treat joint length as mm only if the user explicitly says so
                # via `unit_conventions.joint_length: 'mm'` — fall back to m otherwise.
                pass

        # joint length default is m unless overridden via the special key
        joint_len_unit = units.get("joint_length", "m")
        if "joint_length_m" in series_by_field and joint_len_unit != "m":
            factor = {"km": 1000.0, "ft": 0.3048, "mm": 0.001}.get(
                joint_len_unit.lower(), None
            )
            if factor is None:
                raise ValueError(
                    f"Unrecognised joint_length unit: {joint_len_unit!r}"
                )
            series_by_field["joint_length_m"] = series_by_field[
                "joint_length_m"
            ].astype(float) * factor

        # WT / length / width / altitude default to mm (m for altitude),
        # convert only when units differ from canonical.
        # WT
        wt_unit = units.get("wall_thickness", "mm").lower()
        if "wt_mm" in series_by_field and wt_unit != "mm":
            factor = {"m": 1000.0, "in": 25.4, "inch": 25.4}.get(wt_unit)
            if factor is None:
                raise ValueError(
                    f"Unrecognised wall_thickness unit: {wt_unit!r}"
                )
            series_by_field["wt_mm"] = series_by_field["wt_mm"].astype(float) * factor

        # Length / width
        for canon, unit_key in (
            ("length_mm", "length"),
            ("width_mm", "width"),
        ):
            if canon in series_by_field:
                u = units.get(unit_key, "mm").lower()
                if u != "mm":
                    factor = {"m": 1000.0, "in": 25.4, "inch": 25.4, "cm": 10.0}.get(u)
                    if factor is None:
                        raise ValueError(
                            f"Unrecognised {unit_key} unit: {u!r}"
                        )
                    series_by_field[canon] = (
                        series_by_field[canon].astype(float) * factor
                    )

        # Altitude
        if "altitude_m" in series_by_field:
            u = units.get("altitude", "m").lower()
            if u != "m":
                factor = {"ft": 0.3048, "km": 1000.0}.get(u)
                if factor is None:
                    raise ValueError(f"Unrecognised altitude unit: {u!r}")
                series_by_field["altitude_m"] = (
                    series_by_field["altitude_m"].astype(float) * factor
                )

        # --- depth ----------------------------------------------------------
        depth_unit = units.get("depth", "%").lower()
        if "depth_pct_wt" in series_by_field and depth_unit != "%":
            wt_series = series_by_field.get("wt_mm")
            series_by_field["depth_pct_wt"] = pd.Series(
                _convert_depth_series(
                    series_by_field["depth_pct_wt"], depth_unit, wt_series,
                ),
                index=series_by_field["depth_pct_wt"].index,
            )
        elif "depth_mm" in series_by_field:
            # The user supplied depth in mm via the depth_mm canonical
            # field — convert to %WT and store as depth_pct_wt.
            wt_series = series_by_field.get("wt_mm")
            if wt_series is None:
                raise ValueError(
                    "depth_mm supplied but wt_mm not mapped — need wt to "
                    "convert to %WT"
                )
            series_by_field["depth_pct_wt"] = pd.Series(
                _convert_depth_series(
                    series_by_field.pop("depth_mm"), "mm", wt_series,
                ),
                index=wt_series.index,
            )

        # --- clock ----------------------------------------------------------
        if "clock_position" in series_by_field:
            clk_unit = units.get("clock", "hh:mm")
            series_by_field["clock_position"] = series_by_field[
                "clock_position"
            ].map(lambda v, u=clk_unit: _safe(clock_to_hh_mm, v, u))

        return series_by_field

    # --------------------------------------------------- pipe-sheet support

    def read_pipe_source(self, file_path: str | Path) -> pd.DataFrame | None:
        """Read the optional pipe-registry sheet, returning ``None`` if absent.

        Only fires when ``profile.pipe_sheet_name`` is set. Same shape as
        :meth:`read_source`: header promoted, all-empty rows/cols dropped.

        CSV inputs have no concept of a second sheet — this method
        returns ``None`` for them, so the convert flow silently skips
        the pipe-sheet output. A future enhancement could accept a
        sibling CSV (e.g. ``foo_Pipe.csv``) as the pipe registry; for
        now, users with pipe-registry data should provide an xlsx.
        """
        if not self.profile.pipe_sheet_name:
            return None
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Source file not found: {path}")
        if path.suffix.lower() == ".csv":
            # No second sheet inside a CSV — skip the pipe-registry
            # pass-through. Without this branch, pd.read_excel would
            # raise the same "Excel format cannot be determined" error
            # as the bare convert path used to.
            return None
        engine = _excel_engine_for(path)
        df = pd.read_excel(
            path,
            sheet_name=self.profile.pipe_sheet_name,
            header=self.profile.pipe_header_row,
            engine=engine,
        )
        if isinstance(df, dict):                                  # pragma: no cover
            df = next(iter(df.values()))
        df = df.dropna(axis=1, how="all").dropna(axis=0, how="all")
        return df.reset_index(drop=True)

    def transform_pipe(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply the pipe-sheet column mapping (no unit conversions yet).

        The pipe sheet typically carries: ``joint_number``, ``joint_length_m``,
        ``abs_distance_m``, ``upstream_weld_dist_m``, ``wt_mm``,
        ``latitude``, ``longitude``, plus a free-text ``Feature Type``
        column that the downstream reader uses to recognise weld vs
        joint vs valve rows. We carry that ``Feature Type`` column
        through unchanged when present (under the canonical
        ``feature_type`` name → see ``column_synonyms.yaml``).

        Unit conversions are NOT applied here — vendors that report
        chainage in km/ft on the defect sheet typically use the SAME
        units on the pipe sheet, and that's captured by sharing
        :attr:`profile.unit_conventions`. We re-use ``_convert_units``
        for the few canonical fields that overlap.
        """
        mappings = self.profile.normalised_pipe_mappings()
        units = {**_DEFAULT_UNITS, **(self.profile.unit_conventions or {})}

        canonical_series: dict[str, pd.Series] = {}
        missing: list[str] = []
        for canon, src in mappings.items():
            if src not in df.columns:
                missing.append(f"{canon} -> {src!r}")
                continue
            canonical_series[canon] = df[src].copy()
        if missing:
            raise KeyError(
                "Profile's pipe_column_mappings references columns that "
                "aren't in the pipe sheet: " + "; ".join(missing)
                + f". Available columns: {list(df.columns)!r}"
            )

        # Re-use unit conversion for any overlapping canonical fields.
        canonical_series = self._convert_units(canonical_series, units)

        out = pd.DataFrame()
        # Preserve a sensible ordering on the pipe sheet too.
        for canon in _OUTPUT_ORDER:
            if canon in canonical_series:
                out[NGP_OUTPUT_COLUMNS[canon]] = canonical_series[canon]

        # The reader uses a ``Feature Type`` column to distinguish weld
        # rows from joint rows. If the source has a column we can pass
        # through as feature_type, do so; otherwise the reader's
        # heuristics still mostly work.
        # We expose it as the NGP-canonical "Feature Type" name.
        for src_name in df.columns:
            if str(src_name).strip().lower() in ("feature type", "feature_type"):
                out["Feature Type"] = df[src_name].astype(str).values
                break

        return out.reset_index(drop=True)

    # ------------------------------------------------------------------ write

    def write_ngp_format(
        self,
        df: pd.DataFrame,
        output_path: str | Path,
        sheet_name: str = "Defects",
        pipe_df: pd.DataFrame | None = None,
        pipe_sheet_name: str = "Pipeline Tally",
    ) -> Path:
        """Write the transformed frame(s) as an NGP-readable .xlsx file.

        If ``pipe_df`` is supplied, a second sheet (default name
        "Pipeline Tally" — one of the reader's recognised pipe sheets)
        is written alongside the Defects sheet so the joint aligner
        sees the FULL joint registry. The reader's header-row detector
        picks up row 0 automatically.
        """
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            if pipe_df is not None and not pipe_df.empty:
                pipe_df.to_excel(writer, sheet_name=pipe_sheet_name, index=False)
        return out

    # ---------------------------------------------------------------- convert

    def convert(
        self,
        input_path: str | Path,
        output_path: str | Path,
        sheet_name: str = "Defects",
        pipe_sheet_name: str = "Pipeline Tally",
        *,
        source_df: pd.DataFrame | None = None,
        pipe_df: pd.DataFrame | None = None,
    ) -> Path:
        """Read → transform → write in one call. Returns the output path.

        If the profile defines ``pipe_sheet_name``, the pipe-registry
        sheet is also read, transformed, and written as a second sheet
        in the output workbook.

        Args:
            input_path: Source workbook (xlsx / xls / csv). The GUI
                passes its already-read cached DataFrames via the two
                keyword overrides below, in which case ``input_path``
                is only used for diagnostics — the file isn't re-read.
            output_path: Destination .xlsx.
            sheet_name: Output Defects-sheet name.
            pipe_sheet_name: Output pipe-registry-sheet name.
            source_df: Pre-read defect-sheet DataFrame. When supplied,
                ``read_source(input_path)`` is skipped. The GUI uses
                this to avoid a redundant disk read at export time —
                and crucially, it sidesteps the "pd.read_excel on a
                .csv path" error that bit Prompt 34.
            pipe_df: Pre-read pipe-registry DataFrame. Same semantics
                as ``source_df`` but for the optional pipe sheet.
        """
        raw = source_df if source_df is not None else self.read_source(input_path)
        transformed = self.transform(raw)

        pipe_transformed: pd.DataFrame | None = None
        if self.profile.pipe_sheet_name:
            raw_pipe = (
                pipe_df if pipe_df is not None
                else self.read_pipe_source(input_path)
            )
            if raw_pipe is not None:
                pipe_transformed = self.transform_pipe(raw_pipe)

        return self.write_ngp_format(
            transformed, output_path,
            sheet_name=sheet_name,
            pipe_df=pipe_transformed,
            pipe_sheet_name=pipe_sheet_name,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _safe(fn, value, *args):
    """Wrap a unit-conversion call so empty/NaN cells pass through unchanged.

    The conversion helpers raise on NaN/empty (that's correct for unit
    tests), but in real vendor files plenty of cells are blank and we'd
    rather emit a blank than abort the whole convert. Anything that does
    raise propagates, so genuine bad data still surfaces.
    """
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return fn(value, *args)


def _convert_depth_series(
    depth_series: pd.Series,
    source_unit: str,
    wt_series: pd.Series | None,
) -> list[float | None]:
    """Apply :func:`depth_to_pct_wt` to a pandas series row-by-row.

    Vectorising this is fiddly because the function takes per-row ``wt_mm``
    when the unit is ``mm``. The row-by-row form is still fast enough
    for the file sizes we care about (~100k rows for HMEL).
    """
    out: list[float | None] = []
    if wt_series is not None:
        wt_list = wt_series.tolist()
    else:
        wt_list = [None] * len(depth_series)

    for d, wt in zip(depth_series.tolist(), wt_list):
        if d is None or (isinstance(d, float) and pd.isna(d)):
            out.append(None)
            continue
        if isinstance(d, str) and not d.strip():
            out.append(None)
            continue
        try:
            out.append(depth_to_pct_wt(d, source_unit, wt))
        except ValueError:
            out.append(None)
    return out
