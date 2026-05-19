"""CSV reading helpers for the Format Converter input path.

Vendor CSV deliverables come in a frustrating mix of encodings:

  * Modern NGP exports     → utf-8 (clean ASCII text + a couple of mm² / °).
  * Athena 2018 exports    → latin-1 (the ``°`` in column headers like
    "Latitude [°]" is 0xb0, which bare ``pd.read_csv(path)`` fails to
    decode under the utf-8 default).
  * Windows-era exports    → cp1252 (similar surface chars, different
    bytes for the upper half).
  * Some pre-Office exports → utf-16 with BOM (Excel "Save As CSV
    (Unicode)" defaults).

Rather than make users guess, this helper tries the most-likely
encodings in order until one decodes the whole file. Order matters:

  1. ``utf-8``       — by far the most common; succeeds for clean files.
  2. ``utf-8-sig``   — utf-8 with a BOM (Excel's "CSV UTF-8" option).
  3. ``latin-1``     — Athena 2018 exports + many older European tooling.
  4. ``cp1252``      — Windows default codepage, near-superset of
                       latin-1 with a few extra typographic chars.
  5. ``utf-16``      — Excel's "Save As CSV (Unicode)" output (with BOM).

The helper raises :class:`UnicodeDecodeError` only if every candidate
fails — in practice this means the file isn't text at all (a renamed
binary or a heavily mojibake'd export from a non-Latin codepage like
shift-jis or gb2312, which we'd want to surface loudly).
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd


# Encoding cascade in priority order. Kept short on purpose — anything
# beyond cp1252 in real Athena projects is so rare we'd rather get a
# loud failure than silently misinterpret the bytes.
DEFAULT_CSV_ENCODINGS: tuple[str, ...] = (
    "utf-8",
    "utf-8-sig",
    "latin-1",
    "cp1252",
    "utf-16",
)


# Magic bytes for BOM-marked files. Checked before the cascade so a
# utf-16 file isn't silently mis-decoded as latin-1 (latin-1 accepts
# ANY byte sequence — without the BOM sniff, latin-1 would always
# "win" before utf-16 was even attempted).
_BOM_TO_ENCODING: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf",      "utf-8-sig"),
    (b"\xff\xfe\x00\x00",  "utf-32-le"),
    (b"\x00\x00\xfe\xff",  "utf-32-be"),
    (b"\xff\xfe",          "utf-16-le"),
    (b"\xfe\xff",          "utf-16-be"),
)


def _detect_bom_encoding(path: Path) -> str | None:
    """Return the encoding implied by a BOM at the start of the file, if any."""
    try:
        with path.open("rb") as f:
            head = f.read(4)
    except OSError:
        return None
    for magic, enc in _BOM_TO_ENCODING:
        if head.startswith(magic):
            return enc
    return None


def read_csv_with_encoding_fallback(
    path: str | Path,
    *,
    encodings: Iterable[str] = DEFAULT_CSV_ENCODINGS,
    **read_csv_kwargs,
) -> pd.DataFrame:
    """Read a CSV file, retrying with each candidate encoding.

    A BOM at the start of the file (utf-8-sig / utf-16-le / utf-16-be /
    utf-32-le / utf-32-be) is honoured first — without that, a utf-16
    file would be silently mis-decoded as latin-1 because latin-1
    accepts any byte sequence and "succeeds" before utf-16 is tried.

    Args:
        path: Path to the CSV file.
        encodings: Override the default cascade. The first encoding
            that decodes successfully wins. BOM-detected encoding (if
            any) is prepended automatically.
        **read_csv_kwargs: Forwarded to :func:`pandas.read_csv` (e.g.
            ``header=None``, ``low_memory=False``).

    Returns:
        The parsed DataFrame.

    Raises:
        UnicodeDecodeError: Every encoding in the cascade failed. The
            error message lists everything tried so the user / a future
            maintainer can extend the cascade or convert the file
            up-front.
    """
    p = Path(path)

    # If the file starts with a recognised BOM, try that encoding
    # first. Caller's cascade is preserved as a fallback for cases
    # where the BOM decode produces a DataFrame that pandas can't
    # parse (very rare in practice).
    candidates: list[str] = []
    bom_enc = _detect_bom_encoding(p)
    if bom_enc:
        candidates.append(bom_enc)
    for enc in encodings:
        if enc not in candidates:
            candidates.append(enc)

    tried: list[str] = []
    last_error: Exception | None = None
    for enc in candidates:
        tried.append(enc)
        try:
            # low_memory=False avoids the dtype-guess warning on big
            # vendor files. Caller can still override.
            return pd.read_csv(
                p,
                encoding=enc,
                low_memory=read_csv_kwargs.pop("low_memory", False),
                **read_csv_kwargs,
            )
        except UnicodeDecodeError as e:
            last_error = e
            continue
        except (UnicodeError, LookupError) as e:
            # LookupError → "unknown encoding"; rare but possible if
            # someone hand-overrides the cascade. Treat the same as a
            # decode failure — try the next one.
            last_error = e
            continue

    # Every candidate failed. Raise a fresh UnicodeDecodeError that
    # reads as an action item for the user.
    msg = (
        f"Could not decode {p} with any of: {list(tried)}. "
        f"Last error: {last_error}"
    )
    # UnicodeDecodeError requires (encoding, object, start, end, reason).
    # We synthesise plausible values so callers that catch it by type
    # still work.
    if isinstance(last_error, UnicodeDecodeError):
        raise UnicodeDecodeError(
            last_error.encoding, last_error.object,
            last_error.start, last_error.end, msg,
        ) from last_error
    raise UnicodeDecodeError(
        "utf-8", b"", 0, 1, msg,
    )


__all__ = ["DEFAULT_CSV_ENCODINGS", "read_csv_with_encoding_fallback"]
