"""Tests for src/io/format_converter/csv_input.read_csv_with_encoding_fallback.

Vendor CSV deliverables come in 4-5 different encodings depending on
the source tooling. These tests pin the cascade so a future "let's
just use utf-8 and tell users to convert" simplification can't
silently break Athena 2018-style files.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.io.format_converter.csv_input import (
    DEFAULT_CSV_ENCODINGS,
    read_csv_with_encoding_fallback,
)


def _make_csv(path: Path, content: str, encoding: str) -> Path:
    """Write a CSV file with the given content and encoding."""
    with path.open("w", encoding=encoding, newline="") as f:
        f.write(content)
    return path


class TestLatin1WithDegreeSymbol:
    """Athena 2018-style: ``°`` byte in column headers is 0xb0 (latin-1).

    Bare ``pd.read_csv(path)`` under the utf-8 default chokes with
    "'utf-8' codec can't decode byte 0xb0". The cascade must succeed
    on the 3rd attempt (utf-8 → utf-8-sig → latin-1).
    """

    def test_loads_latin1_csv_with_degree_symbol(self, tmp_path: Path):
        # Use 0xb0 (°) directly to mimic the Athena 2018 export.
        csv = _make_csv(
            tmp_path / "abu_road_run1.csv",
            "Anomaly ID,Latitude [°],Longitude [°]\n"
            "1,23.15,70.20\n"
            "2,23.16,70.21\n",
            encoding="latin-1",
        )
        df = read_csv_with_encoding_fallback(csv)
        assert len(df) == 2
        # The ° must survive the round-trip into a unicode column name.
        cols = list(df.columns)
        assert any("°" in c for c in cols), (
            f"degree symbol missing from columns: {cols}"
        )

    def test_bare_pandas_would_fail_on_this_file(self, tmp_path: Path):
        """Regression coverage — proves the cascade is doing real work."""
        import pandas as pd
        csv = _make_csv(
            tmp_path / "latin1.csv",
            "col1,col2 [°]\n1,2\n",
            encoding="latin-1",
        )
        # Bare utf-8 must fail on the 0xb0 byte.
        with pytest.raises(UnicodeDecodeError):
            pd.read_csv(csv)
        # Our cascade should succeed.
        df = read_csv_with_encoding_fallback(csv)
        assert len(df) == 1


class TestUtf16WithBOM:
    """Excel's 'CSV (Unicode)' option emits UTF-16 LE with a BOM. Some
    NGP exports from older Office versions still surface this. The
    cascade catches it after utf-8 / utf-8-sig / latin-1 / cp1252
    all fail to find sensible record boundaries.
    """

    def test_loads_utf16_with_bom(self, tmp_path: Path):
        csv = _make_csv(
            tmp_path / "utf16.csv",
            "Anomaly ID,Depth\n1,25.0\n2,30.0\n",
            encoding="utf-16",      # writer emits BOM by default
        )
        df = read_csv_with_encoding_fallback(csv)
        assert len(df) == 2
        # Both columns must be present (no mojibake).
        assert "Anomaly ID" in df.columns
        assert "Depth" in df.columns

    def test_loads_utf8_bom(self, tmp_path: Path):
        """utf-8-sig (UTF-8 with BOM) is what Excel writes when you pick
        'CSV UTF-8 (Comma delimited)'. Bare utf-8 leaves the BOM in
        column 0, so the cascade must reach utf-8-sig for clean output.
        """
        csv = _make_csv(
            tmp_path / "utf8bom.csv",
            "Anomaly ID,Depth\n1,25.0\n",
            encoding="utf-8-sig",
        )
        df = read_csv_with_encoding_fallback(csv)
        # First column must NOT carry the BOM character.
        assert list(df.columns)[0] == "Anomaly ID"


class TestCascadeMechanics:
    def test_default_cascade_order(self):
        """utf-8 first, latin-1 third — order matters."""
        assert DEFAULT_CSV_ENCODINGS[0] == "utf-8"
        assert DEFAULT_CSV_ENCODINGS[1] == "utf-8-sig"
        assert "latin-1" in DEFAULT_CSV_ENCODINGS
        # latin-1 must come BEFORE cp1252 (latin-1 is the formally
        # specified one; cp1252 is the Windows superset).
        assert DEFAULT_CSV_ENCODINGS.index("latin-1") < \
               DEFAULT_CSV_ENCODINGS.index("cp1252")

    def test_kwargs_forwarded_to_read_csv(self, tmp_path: Path):
        csv = _make_csv(
            tmp_path / "noheader.csv",
            "1,2,3\n4,5,6\n",
            encoding="utf-8",
        )
        # Force header=None so pandas treats every row as data.
        df = read_csv_with_encoding_fallback(csv, header=None)
        assert df.shape == (2, 3)
        # Without a header, columns are integers.
        assert list(df.columns) == [0, 1, 2]


class TestUndecodableBytes:
    """A CSV that no encoding can decode (e.g. random binary garbage
    or a truncated UTF-16 file). The cascade must surface the failure
    with a clear list of everything tried, not a single cryptic
    UnicodeDecodeError from utf-8.
    """

    def test_raises_with_actionable_message(self, tmp_path: Path):
        path = tmp_path / "garbage.csv"
        # ODD-length bytes starting with 0xff:
        #   * utf-8 / utf-8-sig fail — 0xff is never a valid utf-8 lead byte.
        #   * utf-16-be / utf-16-le fail — they require even-length input.
        #   * utf-32-be / utf-32-le fail — they require length divisible by 4.
        # That leaves latin-1 / cp1252 as the only byte-by-byte
        # encodings that would succeed, so to exercise the raise path
        # we override the cascade to exclude them.
        path.write_bytes(b"\xff\x80\x81\x82\x83")    # 5 bytes, odd
        with pytest.raises(UnicodeDecodeError) as exc:
            read_csv_with_encoding_fallback(
                path,
                encodings=("utf-8", "utf-8-sig", "utf-16-be", "utf-16-le"),
            )
        msg = str(exc.value)
        # The error must list every encoding tried + the last
        # underlying error so a maintainer can extend the cascade.
        assert "Could not decode" in msg
        for enc in ("utf-8", "utf-8-sig", "utf-16-be", "utf-16-le"):
            assert enc in msg, f"cascade error message missing {enc!r}"

    def test_latin1_fallback_accepts_any_bytes(self, tmp_path: Path):
        """latin-1 / cp1252 decoders are byte-by-byte and accept ALL
        256 possible bytes — so when those are in the cascade (the
        default), no real-world file should hit the raise path. This
        test confirms the default cascade succeeds on garbage bytes,
        documenting that the raise is genuinely an edge case (only
        firing when the user explicitly trims the cascade).
        """
        path = tmp_path / "bytes_as_latin1.csv"
        path.write_bytes(b"\xff\xfe\xff\xfe,foo,bar\n1,2,3\n")
        # Default cascade hits latin-1 and returns SOMETHING — possibly
        # mojibake, but no exception.
        df = read_csv_with_encoding_fallback(path)
        # The number of columns can vary depending on how the latin-1
        # decoder splits the high bytes, but the call must not raise
        # and must produce a DataFrame.
        assert df is not None
