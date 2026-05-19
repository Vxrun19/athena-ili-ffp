"""Pure unit-conversion helpers used by the FormatConverter.

These deliberately have no dependencies on pandas or VendorProfile — they
take scalars and a source-unit string, return canonical-unit scalars, and
raise :class:`ValueError` with a clear message on bad inputs. Tests pin
the edge cases.

Canonical units (consistent with the rest of the pipeline):

  * distance / chainage  → metres
  * depth                → percent of wall thickness (0-100)
  * clock orientation    → ``"hh:mm"`` string (the on-disk NGP form;
                            the reader converts this to decimal hours)
  * length / width / WT  → millimetres
"""
from __future__ import annotations

import math
import re
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_float(value: Any, field_name: str) -> float:
    """Coerce a possibly-stringy numeric to float, raising on failure."""
    if value is None:
        raise ValueError(f"{field_name}: value is None")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            raise ValueError(f"{field_name}: value is NaN")
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            raise ValueError(f"{field_name}: value is empty string")
        # Tolerate a trailing % marker for depth-as-percent cells.
        s = s.rstrip("%").strip()
        # Tolerate thousands separators ("1,234.5") and European decimals
        # ("1.234,5") — keep it conservative; only do this when the string
        # looks numeric otherwise.
        if "," in s and "." in s:
            s = s.replace(",", "")
        elif "," in s and re.match(r"^-?\d+,\d+$", s):
            s = s.replace(",", ".")
        try:
            return float(s)
        except ValueError as e:
            raise ValueError(f"{field_name}: cannot parse {value!r} as float") from e
    raise ValueError(
        f"{field_name}: cannot convert {type(value).__name__} {value!r} to float"
    )


def _normalise_unit(unit: str | None, default: str) -> str:
    """Lower-case + strip a unit string, falling back to ``default``."""
    if unit is None or (isinstance(unit, str) and not unit.strip()):
        return default
    return str(unit).strip().lower()


# ---------------------------------------------------------------------------
# Chainage (axial distance along the pipeline)
# ---------------------------------------------------------------------------

# 1 international foot = 0.3048 m exactly (NIST).
_FT_TO_M = 0.3048

_CHAINAGE_FACTORS = {
    "m": 1.0,
    "metre": 1.0,
    "metres": 1.0,
    "meter": 1.0,
    "meters": 1.0,
    "km": 1000.0,
    "kilometre": 1000.0,
    "kilometres": 1000.0,
    "kilometer": 1000.0,
    "kilometers": 1000.0,
    "ft": _FT_TO_M,
    "feet": _FT_TO_M,
    "foot": _FT_TO_M,
}


def chainage_to_m(value: Any, source_unit: str | None = "m") -> float:
    """Convert a chainage / axial-distance value to metres.

    Args:
        value: Numeric or numeric-string. NaN / empty → :class:`ValueError`.
        source_unit: ``"m"`` (default), ``"km"``, or ``"ft"`` (case-insensitive).

    Raises:
        ValueError: bad value or unrecognised unit.
    """
    unit = _normalise_unit(source_unit, "m")
    if unit not in _CHAINAGE_FACTORS:
        raise ValueError(
            f"chainage_to_m: unrecognised source_unit {source_unit!r}; "
            f"expected one of {sorted(set(_CHAINAGE_FACTORS))}"
        )
    raw = _coerce_float(value, "chainage")
    return raw * _CHAINAGE_FACTORS[unit]


# ---------------------------------------------------------------------------
# Depth
# ---------------------------------------------------------------------------

def depth_to_pct_wt(
    value: Any,
    source_unit: str | None = "%",
    wt_mm: float | None = None,
) -> float:
    """Convert a depth value to percent-of-wall-thickness (0..100).

    Args:
        value: Numeric or numeric-string.
        source_unit: One of ``"%"`` / ``"percent"`` / ``"pct"`` (treat as
            already %WT), ``"mm"`` (depth in mm, requires ``wt_mm``), or
            ``"fraction"`` / ``"frac"`` (0..1, multiplied by 100).
        wt_mm: Wall thickness in mm. Required only when ``source_unit="mm"``.

    Returns:
        Depth as a percent of wall thickness, clipped only minimally —
        callers may want to apply their own [0, 100] guard.

    Raises:
        ValueError: bad value, bad unit, or missing ``wt_mm`` for the
            ``mm`` case.
    """
    unit = _normalise_unit(source_unit, "%")
    raw = _coerce_float(value, "depth")

    if unit in ("%", "percent", "pct", "%wt", "pct_wt"):
        return raw

    if unit in ("fraction", "frac"):
        return raw * 100.0

    if unit in ("mm", "millimetre", "millimetres", "millimeter", "millimeters"):
        if wt_mm is None:
            raise ValueError(
                "depth_to_pct_wt: source_unit='mm' requires wt_mm"
            )
        if wt_mm <= 0.0:
            raise ValueError(
                f"depth_to_pct_wt: wt_mm must be > 0 (got {wt_mm})"
            )
        return (raw / wt_mm) * 100.0

    raise ValueError(
        f"depth_to_pct_wt: unrecognised source_unit {source_unit!r}; "
        f"expected one of '%', 'mm', 'fraction'"
    )


# ---------------------------------------------------------------------------
# Clock position
# ---------------------------------------------------------------------------

_HH_MM_RE = re.compile(r"^\s*(\d{1,2})\s*[:.]\s*(\d{1,2})(?::\d{1,2})?\s*$")


def _hh_mm_from_decimal_hours(decimal_hours: float) -> str:
    """Format decimal hours (in [0, 12)) as an ``hh:mm`` string.

    Wraparound is normalised — 12.0 → 00:00.
    """
    # Wrap to [0, 12)
    h = decimal_hours % 12.0
    if h < 0:
        h += 12.0
    hours = int(h)
    minutes = int(round((h - hours) * 60.0))
    if minutes == 60:
        minutes = 0
        hours = (hours + 1) % 12
    return f"{hours:02d}:{minutes:02d}"


def clock_to_hh_mm(value: Any, source_unit: str | None = "hh:mm") -> str:
    """Convert a clock-position value to a canonical ``"hh:mm"`` string.

    Args:
        value: Source value. For ``"hh:mm"`` this is a string like
            ``"03:45"``; otherwise a numeric (decimal hour, degree, radian).
        source_unit: One of ``"hh:mm"`` (default), ``"decimal_hr"`` /
            ``"hours"``, ``"degrees"`` / ``"deg"``, ``"radians"`` / ``"rad"``.

    Returns:
        ``"hh:mm"`` zero-padded.

    Raises:
        ValueError on unparseable values or unknown source units.
    """
    unit = _normalise_unit(source_unit, "hh:mm")

    if unit in ("hh:mm", "h:mm", "hh_mm", "hhmm", "h_min", "h:min"):
        if value is None:
            raise ValueError("clock_to_hh_mm: value is None")
        # Excel can return a datetime.time for hh:mm cells — accept it.
        try:
            import datetime as _dt
            if isinstance(value, _dt.time):
                return f"{value.hour % 12:02d}:{value.minute:02d}"
        except ImportError:                                      # pragma: no cover
            pass
        s = str(value).strip()
        m = _HH_MM_RE.match(s)
        if not m:
            # Some vendors store "3.45" meaning 3:45 — accept the dot
            # variant; also tolerate bare decimal-hour strings.
            try:
                return clock_to_hh_mm(_coerce_float(s, "clock"), "decimal_hr")
            except ValueError:
                raise ValueError(
                    f"clock_to_hh_mm: cannot parse {value!r} as hh:mm"
                ) from None
        hours = int(m.group(1))
        minutes = int(m.group(2))
        if minutes >= 60:
            raise ValueError(
                f"clock_to_hh_mm: minute component out of range in {value!r}"
            )
        # Normalise 12:00 → 00:00 to match the NGP convention.
        if hours == 12 and minutes == 0:
            hours = 0
        if hours > 12 or hours < 0:
            raise ValueError(
                f"clock_to_hh_mm: hour component out of range in {value!r}"
            )
        return f"{hours:02d}:{minutes:02d}"

    if unit in ("decimal_hr", "decimal_hours", "hours", "hr", "decimal"):
        raw = _coerce_float(value, "clock")
        return _hh_mm_from_decimal_hours(raw)

    if unit in ("degrees", "deg", "°"):
        raw = _coerce_float(value, "clock")
        return _hh_mm_from_decimal_hours(raw / 30.0)

    if unit in ("radians", "rad"):
        # 2π rad on the cross-section == 12 clock hours →
        # hours = rad × (12 / 2π) = rad × 6/π
        raw = _coerce_float(value, "clock")
        return _hh_mm_from_decimal_hours(raw * 6.0 / math.pi)

    raise ValueError(
        f"clock_to_hh_mm: unrecognised source_unit {source_unit!r}; "
        f"expected one of 'hh:mm', 'decimal_hr', 'degrees', 'radians'"
    )
