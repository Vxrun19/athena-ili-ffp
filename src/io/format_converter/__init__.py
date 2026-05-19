"""Vendor-format → NGP-format converter.

The existing pipeline (``src/io/ili_reader.py``) only knows how to read the
NGP/Athena Excel layout. In real Athena projects, Run-2 is always
NGP-formatted (we run it), but Run-1 comes from whatever vendor the
client used years ago (Rosen, Baker Hughes PII, NDT Global, T.D.
Williamson, Onstream, …). Rather than teach the reader every vendor's
quirks, we convert Run-1 to NGP up front, then the pipeline runs
unchanged.

Module layout::

    src/io/format_converter/
        profile.py            VendorProfile dataclass + canonical fields
        unit_conversions.py   chainage / depth / clock conversion helpers
        converter.py          FormatConverter — read → transform → write
        auto_detect.py        propose_profile() — fuzzy match against synonyms
        profiles/             Built-in starter profiles (JSON)
"""
from __future__ import annotations

from .profile import (
    CANONICAL_FIELDS,
    REQUIRED_CANONICAL_FIELDS,
    VendorProfile,
)
from .converter import FormatConverter
from .auto_detect import propose_profile

__all__ = [
    "CANONICAL_FIELDS",
    "REQUIRED_CANONICAL_FIELDS",
    "VendorProfile",
    "FormatConverter",
    "propose_profile",
]
