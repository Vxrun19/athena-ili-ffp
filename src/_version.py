"""Version metadata for the Athena ILI FFP Tool.

`__version__` is the human-readable tag (semver-ish).
`__build_date__` is the literal string "auto" in checked-in source;
`packaging/build.bat` rewrites it to the build timestamp during the
PyInstaller build, so the bundled binary stamps its own provenance.

When the tool is run from a source checkout (no PyInstaller build
ran) the literal "auto" survives on disk. In that case
`version_string()` substitutes this file's mtime and appends a
" (source)" tag so the banner makes it obvious the date is a
source-tree fallback, not a real build timestamp.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

__version__ = "0.3.3"
__build_date__ = "auto"


def version_string() -> str:
    """Render a one-line version banner for `--version` output."""
    if __build_date__ == "auto":
        # Source-tree fallback: build.bat didn't run, so __build_date__
        # is still the checked-in sentinel. Stamp the mtime of this
        # file (no git dependency) and tag it "(source)" so the user
        # knows this isn't a PyInstaller build timestamp.
        try:
            mtime = datetime.fromtimestamp(Path(__file__).stat().st_mtime)
            build_label = f"{mtime.strftime('%Y-%m-%d')} (source)"
        except OSError:
            build_label = "auto"
        return f"Athena ILI FFP Tool v{__version__} (build {build_label})"
    return f"Athena ILI FFP Tool v{__version__} (build {__build_date__})"
