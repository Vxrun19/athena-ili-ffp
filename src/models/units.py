"""
Unit conversions and vendor-value parsers.

All conversions terminate in the tool's internal units:
    pressure -> kg/cm²   (Indian pipeline industry standard)
    length / width / WT -> mm
    chainage / distance -> m
    clock position -> decimal hours in [0.0, 12.0)
    surface side -> Surface enum
    depth -> (% WT, mm) tuple

Conversion factors are anchored on SI exact values:
    1 kgf  = 9.80665 N       (exact, CGPM-1901)
    1 cm²  = 1e-4 m²
    => 1 kg/cm²  = 98066.5 Pa  (exact)
    1 bar       = 100000 Pa    (exact)
    1 psi       = 6894.757293168361 Pa
    1 MPa       = 1e6 Pa
"""
from __future__ import annotations

import datetime as _dt
import math
import re
from typing import Any

from . import Surface

# ---------------------------------------------------------------------------
# Pressure conversions
# ---------------------------------------------------------------------------
# Anchored on 1 kg/cm² = 98 066.5 Pa exactly.

_PA_PER_KGCM2 = 98066.5
_PA_PER_BAR = 1.0e5
_PA_PER_PSI = 6894.757293168361
_PA_PER_MPA = 1.0e6

BAR_TO_KGCM2 = _PA_PER_BAR / _PA_PER_KGCM2          # 1.0197162129779283
PSI_TO_KGCM2 = _PA_PER_PSI / _PA_PER_KGCM2          # 0.07030695783268
MPA_TO_KGCM2 = _PA_PER_MPA / _PA_PER_KGCM2          # 10.197162129779283


def bar_to_kgcm2(p_bar: float) -> float:
    return p_bar * BAR_TO_KGCM2


def kgcm2_to_bar(p_kgcm2: float) -> float:
    return p_kgcm2 / BAR_TO_KGCM2


def psi_to_kgcm2(p_psi: float) -> float:
    return p_psi * PSI_TO_KGCM2


def kgcm2_to_psi(p_kgcm2: float) -> float:
    return p_kgcm2 / PSI_TO_KGCM2


def mpa_to_kgcm2(p_mpa: float) -> float:
    return p_mpa * MPA_TO_KGCM2


def kgcm2_to_mpa(p_kgcm2: float) -> float:
    return p_kgcm2 / MPA_TO_KGCM2


# ---------------------------------------------------------------------------
# Clock position parsing
# ---------------------------------------------------------------------------

_CLOCK_HHMM_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{1,2})(?::(\d{1,2}))?\s*$")
_CLOCK_OCLOCK_RE = re.compile(
    r"^\s*(\d{1,2}(?:\.\d+)?)\s*(?:o[''`]?clock|h)?\s*$", re.IGNORECASE
)


def _wrap_clock(hours: float) -> float:
    """Wrap decimal hours into [0.0, 12.0). 12.0 -> 0.0."""
    if not math.isfinite(hours):
        raise ValueError(f"clock position is not finite: {hours!r}")
    h = hours % 12.0
    # math.fmod / % on negatives gives positive in Python, so this is safe.
    return 0.0 if h == 12.0 else h


def parse_clock(value: Any) -> float | None:
    """Parse a vendor-supplied clock position to decimal hours in [0.0, 12.0).

    Convention (locked): strings use hh:mm; numerics are decimal hours.
    So "6.14" (string) == "6:14" == 6h 14m == 6.2333…, but 6.14 (numeric) == 6.14.

    Accepts:
        None / "" / "n/a"        -> None
        "06:14", "6:14", "6.14", "06:14:00"   -> hours + min/60 + sec/3600
        "6 o'clock", "6 oclock", "6h", "06"    -> 6.0
        6, 6.233                  -> 6.233 (already decimal hours)
        180  (degrees)            -> 6.0    (degrees / 30.0, when 12 < val <= 360)

    Normalises 12.0 to 0.0 (clock face wrap).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"clock position cannot be bool: {value!r}")

    # openpyxl returns datetime.time / datetime.datetime for time-formatted cells.
    if isinstance(value, _dt.datetime):
        value = value.time()
    if isinstance(value, _dt.time):
        return _wrap_clock(value.hour + value.minute / 60.0 + value.second / 3600.0)

    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return None
        v = float(value)
        if 0.0 <= v <= 12.0:
            return _wrap_clock(v)
        if 12.0 < v <= 360.0:
            return _wrap_clock(v / 30.0)
        raise ValueError(
            f"numeric clock position out of range (0–360): {value!r}"
        )

    if isinstance(value, str):
        s = value.strip()
        if s == "" or s.lower() in {"n/a", "na", "none", "null", "-"}:
            return None

        m = _CLOCK_HHMM_RE.match(s)
        if m:
            hh = int(m.group(1))
            mm = int(m.group(2))
            ss = int(m.group(3)) if m.group(3) else 0
            if not (0 <= hh <= 12 and 0 <= mm < 60 and 0 <= ss < 60):
                raise ValueError(f"clock components out of range: {value!r}")
            return _wrap_clock(hh + mm / 60.0 + ss / 3600.0)

        m = _CLOCK_OCLOCK_RE.match(s)
        if m:
            return parse_clock(float(m.group(1)))

        # Last try: plain numeric string
        try:
            return parse_clock(float(s))
        except ValueError as e:
            raise ValueError(f"unrecognised clock format: {value!r}") from e

    raise TypeError(f"unsupported clock value type: {type(value).__name__}")


# ---------------------------------------------------------------------------
# Surface parsing
# ---------------------------------------------------------------------------

_SURFACE_MAP: dict[str, Surface] = {
    "int": Surface.INTERNAL,
    "int.": Surface.INTERNAL,
    "internal": Surface.INTERNAL,
    "i": Surface.INTERNAL,
    "in": Surface.INTERNAL,
    "inner": Surface.INTERNAL,
    "inside": Surface.INTERNAL,
    "ext": Surface.EXTERNAL,
    "ext.": Surface.EXTERNAL,
    "external": Surface.EXTERNAL,
    "e": Surface.EXTERNAL,
    "ex": Surface.EXTERNAL,
    "outer": Surface.EXTERNAL,
    "out": Surface.EXTERNAL,
    "outside": Surface.EXTERNAL,
    "mid": Surface.MIDWALL,
    "midwall": Surface.MIDWALL,
    "mid-wall": Surface.MIDWALL,
    "mw": Surface.MIDWALL,
    "m": Surface.MIDWALL,
}


def parse_surface(value: Any) -> Surface:
    """Normalise vendor surface-side text to a Surface enum.

    Unknown / blank / N/A values map to Surface.UNKNOWN.
    """
    if value is None:
        return Surface.UNKNOWN
    if isinstance(value, Surface):
        return value
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            return Surface.UNKNOWN

    key = value.strip().lower().rstrip(":")
    if key == "" or key in {"n/a", "na", "undefined", "unk", "?", "unknown"}:
        return Surface.UNKNOWN
    return _SURFACE_MAP.get(key, Surface.UNKNOWN)


# ---------------------------------------------------------------------------
# Depth parsing
# ---------------------------------------------------------------------------

def parse_depth(value: Any, wt_mm: float | None) -> tuple[float | None, float | None]:
    """Parse a vendor depth value into (depth_pct_wt, depth_mm).

    Accepts:
        None / "" / "n/a"   -> (None, None)
        "28.5%"             -> (28.5, 0.285 * wt_mm)
        "28.5"              -> (28.5, ...)         # bare numeric >=1 is %
        28.5                -> (28.5, ...)
        0.285               -> (28.5, ...)         # numeric in (0, 1) is fraction
        0                   -> (0.0, 0.0)
        1.0                 -> ambiguous: treated as 1.0% (bare-number convention)

    depth_mm is only returned when wt_mm is a positive number; otherwise it is None.
    """
    if value is None:
        return (None, None)
    if isinstance(value, bool):
        raise TypeError(f"depth cannot be bool: {value!r}")

    is_percent_marked = False

    if isinstance(value, str):
        s = value.strip()
        if s == "" or s.lower() in {"n/a", "na", "none", "null", "-"}:
            return (None, None)
        if s.endswith("%"):
            is_percent_marked = True
            s = s[:-1].strip()
        try:
            num = float(s)
        except ValueError as e:
            raise ValueError(f"unrecognised depth value: {value!r}") from e
    elif isinstance(value, (int, float)):
        num = float(value)
    else:
        raise TypeError(f"unsupported depth value type: {type(value).__name__}")

    if not math.isfinite(num):
        return (None, None)

    if is_percent_marked:
        pct = num
    elif 0.0 < num < 1.0:
        # Bare fraction in (0, 1) -> interpret as fraction of WT.
        pct = num * 100.0
    else:
        pct = num

    if not (0.0 <= pct <= 100.0):
        raise ValueError(f"depth out of range after parse: {pct} (from {value!r})")

    depth_mm: float | None
    if isinstance(wt_mm, (int, float)) and wt_mm is not None and wt_mm > 0:
        depth_mm = pct / 100.0 * float(wt_mm)
    else:
        depth_mm = None

    return (pct, depth_mm)
